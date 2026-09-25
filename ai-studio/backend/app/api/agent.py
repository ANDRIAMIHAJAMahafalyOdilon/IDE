"""Endpoints de l'agent : chat (SSE) + application des modifications validées.

Le flux SSE est totalement sûr : toute exception de la génération est
convertie en événement `erreur` {code, message, fichier?} — jamais de
fermeture silencieuse. Séquence prévisible :
    debut → (texte | proposition)* → (erreur | fin)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from ..models.chat import RequeteApply, RequeteChat, RequetePermission, RequeteTache, RequeteTacheControle
from ..services import (
    agent_adapter, agent_chat, agent_discussion, agent_tache, application, moteurs,
    opencode, workspace,
)
from ..services.diff import ErreurDiff

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Mémoire de session, indexée par (mode, id logique) : les fils « chat » et
# « edit » sont ISOLÉS (le bavardage général ne doit jamais polluer le prompt
# d'édition, ni l'inverse). Sessions OpenCode réutilisées par projet.
_MEMOIRE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_SESSIONS_OPENCODE: dict[str, str] = {}
# Sessions OpenCode du MODE AUTONOME : la conversation vit du côté OpenCode,
# on la "forke" à chaque tâche pour garder la continuité du projet.
_SESSIONS_TACHE: dict[str, str] = {}


def _evt(nom: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": nom, "data": json.dumps(data, ensure_ascii=False)}


def _err(code: str, message: str, fichier: str | None = None) -> dict[str, str]:
    return _evt("erreur", {"code": code, "message": message, "fichier": fichier})


def _memoire(mode: str, sid: str) -> list[dict[str, Any]]:
    """Mémoire du fil (mode, session) — créée à la demande."""
    return _MEMOIRE.setdefault((mode, sid), [])


def _session_id(mode: str, lié: str | None) -> str:
    if lié and (mode, lié) in _MEMOIRE:
        return lié
    sid = lié or f"sess-{uuid.uuid4().hex[:8]}"
    _MEMOIRE.setdefault((mode, sid), [])
    return sid


@router.post("/chat")
async def chat(req: RequeteChat) -> EventSourceResponse:
    """Streaming SSE. `mode="chat"` : discussion libre. `mode="edit"` : tokens
    du moteur + propositions en hunks (jamais de contenu brut à comparer côté
    frontend — décision d'architecture option A)."""
    if req.mode == "chat":
        return EventSourceResponse(generer_discussion(req))
    if not req.projet:
        raise HTTPException(status_code=422, detail="projet requis en mode edit")
    racine = workspace.racine_projet(req.projet)
    return EventSourceResponse(generer_evenements(req, racine))


def _contexte_discussion(req: RequeteChat) -> tuple[str, str]:
    """Contexte projet en LECTURE SEULE, seulement s'il est disponible.

    Le Chat fonctionne sans projet ouvert : dans ce cas aucun contexte n'est
    ajouté au prompt.
    """
    if not req.projet:
        return "", ""
    try:
        racine = workspace.projet_existant(req.projet)
    except (FileNotFoundError, ValueError):
        return "", ""
    return agent_discussion.construire_contexte(racine, req.fichiers_contexte)


async def generer_discussion(req: RequeteChat) -> Any:
    """Événementiel du mode Chat : debut → texte* → (erreur | fin).

    Aucune `proposition` n'est émise et aucun code d'erreur fichier
    (parse_format/schema_invalide/hors_projet) ne peut survenir.
    """
    session = _session_id("chat", req.session)
    memoire = _memoire("chat", session)
    try:
        arborescence, bloc = _contexte_discussion(req)
        if req.documents:
            bloc_documents = agent_discussion.construire_bloc_documents(req.message)
        else:
            bloc_documents = ""
        if req.web:
            bloc_web = agent_discussion.construire_bloc_web(req.message)
        else:
            bloc_web = ""
        prompt = agent_discussion.construire_prompt(
            req.message,
            arborescence,
            bloc,
            agent_discussion.construire_memoire(memoire),
            bloc_documents,
            bloc_web,
        )
        moteur, flux = agent_discussion.demarrer_reponse(prompt)
    except moteurs.ErreurMoteur as exc:
        yield _err(exc.code, exc.message)
        return
    except Exception as exc:  # noqa: BLE001 — jamais de fermeture muette
        yield _err("interne", f"Erreur interne du backend : {exc}")
        return

    yield _evt("debut", {"session": session, "autoris": False, "moteur": moteur})
    morceaux: list[str] = []
    try:
        for delta in flux:
            morceaux.append(delta)
            yield _evt("texte", {"delta": delta})
    except moteurs.ErreurMoteur as exc:
        yield _err(exc.code, exc.message)
        return
    except Exception as exc:  # noqa: BLE001 — jamais de fermeture muette
        yield _err("interne", f"Erreur interne du backend : {exc}")
        return

    memoire.append({"question": req.message, "reponse": "".join(morceaux)})
    del memoire[:-20]
    yield _evt("fin", {"session": session, "nb_fichiers": 0})


async def generer_evenements(req: RequeteChat, racine: Path) -> Any:
    """Génère l'événementiel du chat. Sortie de la route pour test direct via
    asyncio.run (sans HTTP), mais branchée telle quelle sur le SSE réel."""
    session = _session_id("edit", req.session)
    try:
        if req.simulation:
            yield _evt("debut", {
                "session": session, "autoris": req.autoriser_modifications,
                "moteur": "simulation",
            })
            for prop in agent_chat.creer_propositions(
                racine, [s.model_dump() for s in req.simulation]
            ):
                yield _evt("proposition", prop)
            yield _evt("fin", {"session": session, "nb_fichiers": len(req.simulation) or 0})
            return

        # ── Moteur réel : session OpenCode réutilisée (proposeur seul) ──
        try:
            sid = _SESSIONS_OPENCODE.get(req.projet)
            if not sid:
                sid = opencode.creer_session(racine)
                _SESSIONS_OPENCODE[req.projet] = sid
        except opencode.ErreurOpenCode:
            sid = None  # le fallback Gemini/Groq prend le relais

        yield _evt("debut", {
            "session": session, "autoris": req.autoriser_modifications,
            "moteur": "opencode" if sid else "llm",
        })

        resultat = agent_chat.generer_propositions_moteur(
            racine,
            req.message,
            req.fichiers_contexte,
            _memoire("edit", session),
            sid,
        )
        if resultat["texte_resume"]:
            yield _evt("texte", {"delta": resultat["texte_resume"]})
        for prop in resultat["propositions"]:
            yield _evt("proposition", prop)
        if not resultat["propositions"]:
            yield _evt(
                "texte",
                {"delta": "Aucune modification nécessaire pour cette consigne."},
            )

        _memoire("edit", session).append({
            "question": req.message,
            "reponse": resultat["texte_resume"],
        })
        del _memoire("edit", session)[:-20]
        yield _evt(
            "fin",
            {"session": session, "nb_fichiers": len(resultat["propositions"])},
        )
    except agent_adapter.ErreurAdaptateur as exc:
        yield _err(exc.code, exc.message, fichier=exc.fichier)
    except workspace.CheminHorsProjet as exc:
        yield _err("hors_projet", str(exc))
    except Exception as exc:  # noqa: BLE001 — garde-fou : jamais de fermeture muette
        yield _err("interne", f"Erreur interne du backend : {exc}")


@router.post("/tache")
async def tache(req: RequeteTache) -> EventSourceResponse:
    """Mode autonomie : OpenCode exécute réellement la consigne dans le
    dossier du projet (fichiers + commandes), streamé en SSE.

    Séquence : debut -> (texte|outil)* -> (erreur | fin). Le frontend relit
    les fichiers modifiés via GET /api/projects/{projet}/etat (poll).

    Le mode autonome est également disponible pour les dossiers ouverts en
    mode DIRECT : OpenCode travaille alors sur le vrai dossier sélectionné,
    comme lorsqu'il est lancé depuis un terminal.
    """
    racine = workspace.projet_existant(req.projet)
    return EventSourceResponse(generer_tache(req, racine))


@router.post("/tache/abort")
async def tache_abort(req: RequeteTacheControle):
    """Interrompt réellement la session OpenCode du projet."""
    sid = _SESSIONS_TACHE.get(req.projet)
    if not sid:
        return {"ok": True, "interrompue": False}
    try:
        racine = workspace.projet_existant(req.projet)
        opencode.interrompre_session(racine, sid)
        return {"ok": True, "interrompue": True}
    except opencode.ErreurOpenCode as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/tache/permission")
async def tache_permission(req: RequetePermission):
    """Répond à une demande de permission OpenCode en attente."""
    try:
        racine = workspace.projet_existant(req.projet)
        return opencode.repondre_permission(
            racine, req.request_id, req.reply, req.message
        )
    except opencode.ErreurOpenCode as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except workspace.CheminHorsProjet as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def generer_tache(req: RequeteTache, racine: Path) -> Any:
    """Événementiel du mode autonomie (sortie directe asyncio, comme le chat)."""
    try:
        sid_opencode = None
        try:
            sid_opencode = _SESSIONS_TACHE.get(req.projet)
        except Exception:  # noqa: BLE001 — jamais bloquant en lecture
            pass
        async for nom, data in agent_tache.executer_tache(
            racine, req.message, sid_opencode=sid_opencode
        ):
            # Persistance de la session complète (fork suivant) ; affichage court.
            if nom in ("debut", "fin") and data.get("session"):
                _SESSIONS_TACHE[req.projet] = data["session"]
                data["session"] = str(data["session"])[:8]
            yield _evt(nom, data)
        _memoire("tache", req.session or req.projet).append(
            {"question": req.message, "reponse": "tâche exécutée (mode autonome)"}
        )
    except agent_tache.ErreurTache as exc:
        yield _err(exc.code, exc.message)
    except workspace.CheminHorsProjet as exc:
        yield _err("hors_projet", str(exc))
    except Exception as exc:  # noqa: BLE001 — garde-fou : jamais de fermeture muette
        yield _err("interne", f"Erreur interne du backend : {exc}")


@router.post("/apply-changes")
async def apply_changes(req: RequeteApply):
    """Applique sur disque les hunks validés par l'utilisateur (accept/reject).

    `source_hash` protège contre l'application d'une proposition périmée :
    un fichier modifié entre-temps est signalé par fichier (statut "erreur").
    """
    racine = workspace.racine_projet(req.projet)
    try:
        resultats = application.appliquer_changements(
            racine, [m.model_dump() for m in req.modifications]
        )
    except workspace.CheminHorsProjet as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ErreurDiff as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"resultats": [r.__dict__ for r in resultats]}