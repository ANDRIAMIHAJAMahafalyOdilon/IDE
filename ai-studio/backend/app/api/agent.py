"""Endpoints de l'agent : chat (SSE) + application des modifications validées.

Le flux SSE est totalement sûr : toute exception de la génération est
convertie en événement `erreur` {code, message, fichier?} — jamais de
fermeture silencieuse. Séquence prévisible :
    debut → (texte | proposition)* → (erreur | fin)
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import uuid
from dataclasses import dataclass, field
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
from ..config import MEMOIRE_DIR

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Mémoire de session, indexée par (mode, id logique) : les fils « chat » et
# « edit » sont ISOLÉS (le bavardage général ne doit jamais polluer le prompt
# d'édition, ni l'inverse). Les sessions OpenCode sont propres à chaque fil.
_MEMOIRE: dict[tuple[str, str], list[dict[str, Any]]] = {}
_SESSIONS_OPENCODE: dict[tuple[str, str], str] = {}
# Sessions OpenCode du MODE AUTONOME : la conversation vit du côté OpenCode et
# est reprise par le jeton stable du fil frontend.
_SESSIONS_TACHE: dict[tuple[str, str], str] = {}


@dataclass
class EtatTache:
    """État d'une tâche indépendant de la connexion SSE du navigateur."""

    projet: str
    session: str
    message: str
    evenements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    terminee: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    travail: asyncio.Task[None] | None = None


_TACHES: dict[tuple[str, str], EtatTache] = {}


def _evt(nom: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": nom, "data": json.dumps(data, ensure_ascii=False)}


def _err(code: str, message: str, fichier: str | None = None) -> dict[str, str]:
    return _evt("erreur", {"code": code, "message": message, "fichier": fichier})


def _memoire(mode: str, sid: str) -> list[dict[str, Any]]:
    """Mémoire du fil (mode, session) — créée à la demande."""
    cle = (mode, sid)
    if cle in _MEMOIRE:
        return _MEMOIRE[cle]
    nom = hashlib.sha256(f"{mode}:{sid}".encode("utf-8")).hexdigest() + ".json"
    chemin = MEMOIRE_DIR / nom
    memoire: list[dict[str, Any]] = []
    try:
        valeur = json.loads(chemin.read_text(encoding="utf-8"))
        if isinstance(valeur, list):
            memoire = [x for x in valeur if isinstance(x, dict)][-40:]
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    _MEMOIRE[cle] = memoire
    return memoire


def _sauver_memoire(mode: str, sid: str, memoire: list[dict[str, Any]]) -> None:
    """Sauvegarde atomique du fil pour survivre aux redémarrages backend."""
    nom = hashlib.sha256(f"{mode}:{sid}".encode("utf-8")).hexdigest() + ".json"
    chemin = MEMOIRE_DIR / nom
    temporaire = chemin.with_suffix(".tmp")
    try:
        MEMOIRE_DIR.mkdir(parents=True, exist_ok=True)
        temporaire.write_text(
            json.dumps(memoire[-40:], ensure_ascii=False), encoding="utf-8"
        )
        temporaire.replace(chemin)
    except OSError:
        # La mémoire RAM reste active si le dossier de données est momentanément
        # indisponible (droits, disque amovible, etc.).
        pass


def _session_id(mode: str, lié: str | None) -> str:
    sid = lié or f"sess-{uuid.uuid4().hex[:8]}"
    # Passe par le chargeur durable : après un redémarrage backend, un session
    # id déjà connu ne doit pas repartir avec une mémoire vide.
    _memoire(mode, sid)
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
    _sauver_memoire("chat", session, memoire)
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
            cle_session = (req.projet, session)
            sid = _SESSIONS_OPENCODE.get(cle_session)
            if not sid:
                sid = opencode.creer_session(racine)
                _SESSIONS_OPENCODE[cle_session] = sid
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
        _sauver_memoire("edit", session, _memoire("edit", session))
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
    session = req.session or f"tache-{uuid.uuid4().hex[:12]}"
    cle = (req.projet, session)
    etat = _TACHES.get(cle)

    # Un refresh renvoie une consigne vide : on se rattache à la tâche déjà
    # présente et on rejoue son journal. Une nouvelle consigne après une tâche
    # terminée ouvre naturellement un nouveau tour dans la même session.
    if etat is None or (etat.terminee and req.message.strip()):
        if not req.message.strip():
            return EventSourceResponse(_flux_tache_absent())
        etat = EtatTache(req.projet, session, req.message)
        _TACHES[cle] = etat
        etat.travail = asyncio.create_task(_executer_tache(etat, racine))
    return EventSourceResponse(_flux_tache(etat))


async def _flux_tache_absent() -> Any:
    yield _err("tache_introuvable", "Aucune tâche autonome à reprendre.")


async def _ajouter_evenement_tache(
    etat: EtatTache, nom: str, data: dict[str, Any]
) -> None:
    data = dict(data)
    if nom in ("debut", "fin") and data.get("session"):
        _SESSIONS_TACHE[(etat.projet, etat.session)] = str(data["session"])
        # Le frontend garde ce jeton stable ; l'identifiant OpenCode réel reste
        # uniquement dans le backend pour la reprise du prochain tour.
        data["session"] = etat.session
    async with etat.condition:
        etat.evenements.append((nom, data))
        etat.condition.notify_all()


async def _executer_tache(etat: EtatTache, racine: Path) -> None:
    try:
        sid_opencode = _SESSIONS_TACHE.get((etat.projet, etat.session))
        async for nom, data in agent_tache.executer_tache(
            racine, etat.message, sid_opencode=sid_opencode
        ):
            await _ajouter_evenement_tache(etat, nom, data)
        memoire = _memoire("tache", etat.session)
        memoire.append(
            {"question": etat.message, "reponse": "tâche exécutée (mode autonome)"}
        )
        _sauver_memoire("tache", etat.session, memoire)
    except agent_tache.ErreurTache as exc:
        await _ajouter_evenement_tache(etat, "erreur", {
            "code": exc.code,
            "message": exc.message,
            "fichier": None,
        })
    except workspace.CheminHorsProjet as exc:
        await _ajouter_evenement_tache(etat, "erreur", {
            "code": "hors_projet",
            "message": str(exc),
            "fichier": None,
        })
    except Exception as exc:  # noqa: BLE001 — garde-fou du travail détaché
        await _ajouter_evenement_tache(etat, "erreur", {
            "code": "interne",
            "message": f"Erreur interne du backend : {exc}",
            "fichier": None,
        })
    finally:
        async with etat.condition:
            etat.terminee = True
            etat.condition.notify_all()


async def _flux_tache(etat: EtatTache) -> Any:
    index = 0
    while True:
        while index < len(etat.evenements):
            nom, data = etat.evenements[index]
            index += 1
            yield _evt(nom, data)
        if etat.terminee:
            return
        async with etat.condition:
            if index >= len(etat.evenements) and not etat.terminee:
                await etat.condition.wait()


@router.get("/tache/status")
async def tache_status(projet: str, session: str):
    etat = _TACHES.get((projet, session))
    if etat is None:
        return {"existe": False, "active": False, "terminee": False}
    return {
        "existe": True,
        "active": not etat.terminee,
        "terminee": etat.terminee,
        "message": etat.message,
    }


@router.post("/tache/abort")
async def tache_abort(req: RequeteTacheControle):
    """Interrompt réellement la session OpenCode du projet."""
    sid = next(
        (session for (projet, _), session in _SESSIONS_TACHE.items() if projet == req.projet),
        None,
    )
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
