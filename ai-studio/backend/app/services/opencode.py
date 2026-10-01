"""Client OpenCode local (proposeur-seul) via le SDK officiel `opencode-ai`.

L'agent OpenCode ne touche JAMAIS au disque ici : il reçoit un prompt qui lui
interdit toute écriture et lui demande une sortie structurée (JSON ou blocs
===FILE===). La session est créée avec ce system prompt, puis chaque tour est
envoyé en un seul message (le contexte passe par le prompt, pas par
l'historique OpenCode — on garde une session par projet, recréée si perdue).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from ..config import (
    OPENCODE_AGENT_BASE_URL,
    OPENCODE_BASE_URL,
    OPENCODE_CONFIG,
    OPENCODE_TIMEOUT,
)

logger = logging.getLogger(__name__)


class ErreurOpenCode(Exception):
    """Serveur OpenCode injoignable, ou retour d'erreur HTTP."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SYSTEM_PROMPT = (
    "Tu es l'assistant d'édition d'un IDE. Tu ne peux PAS lire ni écrire de "
    "fichier, ni lancer de commande : tu travailles uniquement à partir du "
    "contexte fourni dans le message. Quand une modification est demandée, "
    "propose les fichiers complets à écrire ou à supprimer. Réponds STRICTEMENT "
    "en JSON : une liste d'objets de la forme "
    '{"action": "write", "fichier": "chemin/relatif.ext", "contenu": "fichier '
    'complet"} ou {"action": "delete", "fichier": "chemin/relatif.ext"}. '
    "Aucun texte hors de cette liste, aucun bloc de code markdown autour."
)


def _client(timeout: float = OPENCODE_TIMEOUT, agent: bool = False) -> httpx.Client:
    base = OPENCODE_AGENT_BASE_URL if agent else OPENCODE_BASE_URL
    return httpx.Client(base_url=base.rstrip("/"), timeout=timeout)


def verifier_serveur() -> bool:
    return _sante(OPENCODE_BASE_URL)


def verifier_serveur_agent() -> bool:
    return _sante(OPENCODE_AGENT_BASE_URL)


def _sante(base: str) -> bool:
    try:
        return httpx.get(f"{base}/global/health", timeout=3).is_success
    except Exception:
        return False


def creer_session(
    directory: str | Path, title: str | None = None, agent: bool = False
) -> str:
    """Crée une session OpenCode et retourne son id.

    Le contrat serveur est `POST /session { parentID?, title? }` : le system
    prompt N'EST PAS accepté ici. Il est transmis à chaque tour, dans le corps
    du message (cf. `envoyer_instruction`).
    """
    try:
        resp = _client(15, agent=agent).post(
            "/session",
            params={"directory": str(Path(directory).resolve())},
            headers={"x-opencode-directory": str(Path(directory).resolve())},
            json={"title": title} if title else {},
        )
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        raise ErreurOpenCode(
            "serveur_indisponible",
            f"Serveur OpenCode injoignable sur {OPENCODE_BASE_URL} "
            f"(lancer `opencode serve --port 4096`). {exc}",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ErreurOpenCode(
            "serveur_indisponible",
            f"Erreur {exc.response.status_code} lors de la création de session.",
        ) from exc
    return resp.json()["id"]


def envoyer_instruction(sid: str, directory: str | Path, texte: str) -> str:
    """Envoie un tour de proposeur. Retourne le texte final de l'agent.

    `system` est la clé qui fait tenir le contrat « proposeur seul » : sans
    elle, OpenCode reçoit une consigne d'édition sans aucune interdiction
    d'écrire et peut modifier le projet alors que l'utilisateur n'a accepté
    qu'une proposition. Le contrat serveur
    `POST /session/:id/message { messageID?, model?, agent?, noReply?, system?,
    tools?, parts }` l'accepte à chaque tour — d'où l'envoi systématique.
    """
    try:
        resp = _client().post(
            f"/session/{sid}/message",
            json={
                "system": SYSTEM_PROMPT,
                "parts": [{"type": "text", "text": texte}],
            },
            headers={"x-opencode-directory": str(Path(directory).resolve())},
        )
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        raise ErreurOpenCode(
            "serveur_indisponible", f"Serveur OpenCode injoignable : {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ErreurOpenCode(
            "timeout", f"OpenCode n'a pas répondu en {int(OPENCODE_TIMEOUT)} s."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ErreurOpenCode(
            "serveur_indisponible",
            f"Erreur {exc.response.status_code} lors de l'envoi.",
        ) from exc

    return _extraire_texte_final(resp.json())


def _extraire_texte_final(resultat: dict) -> str:
    """Texte final de l'agent (parties `text` et `agent`/`step` si présentes)."""
    morceaux: list[str] = []
    for partie in resultat.get("parts", []):
        genre = partie.get("type")
        if genre == "text":
            morceaux.append(partie.get("text", ""))
        elif genre in {"agent", "step"} and isinstance(partie.get("text"), str):
            morceaux.append(partie["text"])
    return "\n".join(m for m in morceaux if m).strip()

# ─────────────────────── Exécution autonome headless ───────────────────────

_SERVEUR_PROCESS: subprocess.Popen | None = None
# Serveur du mode autonome, sur un port DIFFERENT. Deux processus distincts
# signifie deux environnements distincts : c'est la garantie structurelle que
# `AISTUDIO_AGENT` ne peut pas atteindre la session interactive de l'utilisateur.
_SERVEUR_AGENT_PROCESS: subprocess.Popen | None = None


def _serveur_binaire() -> str:
    """Résout le même binaire que le mode terminal."""
    from . import agent_tache
    return agent_tache.resoudre_binaire()


def _serveur_params(directory: str | Path) -> tuple[str, dict[str, str]]:
    racine = str(Path(directory).resolve())
    return racine, {"directory": racine}


def _env_serveur(agent: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    # La config de l'application porte les instructions et les permissions de
    # l'agent. Elle n'est pas auto-découverte : le `cwd` du serveur est le
    # projet de l'utilisateur, donc OpenCode lirait la config de CE projet.
    if OPENCODE_CONFIG.is_file():
        env["OPENCODE_CONFIG"] = str(OPENCODE_CONFIG.resolve())
    if agent:
        # Le plugin `agent_fond` ne réécrit une commande longue en arrière-plan
        # que si ces variables sont présentes. Elles vivent dans l'env du processus
        # serveur, jamais dans un fichier de config : une session interactive
        # lancée par l'utilisateur ne peut pas les hériter.
        env["AISTUDIO_AGENT"] = "1"
        try:
            from .agent_fond import chemin_lanceur, dossier_agents

            env["AISTUDIO_LANCEUR"] = str(chemin_lanceur())
            env["AISTUDIO_LOG_DIR"] = str(dossier_agents())
        except Exception:  # noqa: BLE001 — sans lanceur, pas de réécriture
            logger.debug("Lancement en arrière-plan indisponible", exc_info=True)
    return env


def _demarrer_serveur(
    directory: str | Path, base: str, agent: bool
) -> subprocess.Popen:
    """Démarre un serveur sur `base` et attend qu'il réponde."""
    cible = urlparse(base)
    hote = cible.hostname or "127.0.0.1"
    port = cible.port or 4096
    try:
        proc = subprocess.Popen(
            [_serveur_binaire(), "serve", "--hostname", hote, "--port", str(port)],
            cwd=str(Path(directory).resolve()),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=_env_serveur(agent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ErreurOpenCode) as exc:
        raise ErreurOpenCode(
            "moteur_indisponible", f"Impossible de lancer le serveur OpenCode : {exc}"
        ) from exc
    limite = time.monotonic() + 15
    while time.monotonic() < limite:
        if _sante(base):
            return proc
        if proc.poll() is not None:
            raise ErreurOpenCode(
                "moteur_indisponible",
                f"Le serveur OpenCode s'est arrêté pendant le démarrage "
                f"(code {proc.returncode}). Sur Windows, EEXIST indique un bug de "
                "création du dossier de configuration OpenCode.",
            )
        time.sleep(0.25)
    proc.kill()
    raise ErreurOpenCode(
        "serveur_indisponible",
        f"Le serveur OpenCode ne répond pas sur {base}.",
    )


def assurer_serveur(directory: str | Path) -> None:
    """Serveur du mode discussion (port 4096), sans aucun flag agent."""
    global _SERVEUR_PROCESS
    if verifier_serveur():
        return
    if _SERVEUR_PROCESS is not None and _SERVEUR_PROCESS.poll() is not None:
        _SERVEUR_PROCESS = None
    _SERVEUR_PROCESS = _demarrer_serveur(directory, OPENCODE_BASE_URL, agent=False)


def assurer_serveur_agent(directory: str | Path) -> None:
    """Serveur DÉDIÉ au mode autonome (port 4097), avec `AISTUDIO_AGENT`.

    Volontairement distinct de `assurer_serveur`, sur un autre port : c'est cette
    séparation qui rend impossible l'activation du plugin chez l'utilisateur.
    Un serveur déjà lancé par AI Studio sur ce port est considéré comme le
    nôtre et réutilisé : il porte déjà le flag.
    """
    global _SERVEUR_AGENT_PROCESS
    if verifier_serveur_agent():
        return
    if _SERVEUR_AGENT_PROCESS is not None and _SERVEUR_AGENT_PROCESS.poll() is not None:
        _SERVEUR_AGENT_PROCESS = None
    _SERVEUR_AGENT_PROCESS = _demarrer_serveur(
        directory, OPENCODE_AGENT_BASE_URL, agent=True
    )


def arreter_serveur() -> None:
    """Arrête les serveurs OpenCode lancés par CE processus.

    Sans cela, chaque arrêt/redémarrage du backend laisse un `opencode serve`
    orphelin qui garde le port occupé : le redémarrage suivant échoue
    silencieusement côté `assurer_serveur`. Un serveur déjà lancé par
    l'utilisateur (`_SERVEUR_PROCESS is None`) n'est jamais tué.
    """
    global _SERVEUR_PROCESS, _SERVEUR_AGENT_PROCESS
    for attr in ("_SERVEUR_PROCESS", "_SERVEUR_AGENT_PROCESS"):
        proc = globals().get(attr)
        globals()[attr] = None
        if proc is None or proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass


def _sse_payload(data: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("payload"), dict):
        return obj["payload"]
    return obj if isinstance(obj, dict) else None


async def flux_tache(
    directory: str | Path, sid: str, texte: str,
    agent: bool = False, timeout: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Diffuse les événements OpenCode d'une tâche jusqu'à sa fin.

    `timeout` : délai d'attente d'un ÉVÉNEMENT, pas de la tâche. La valeur par
    défaut (90 s) tuerait une tâche légitime dont l'agent réfléchit longuement
    sans rien émettre ; l'appelant passe donc son propre budget.
    """
    racine, params = _serveur_params(directory)
    base = OPENCODE_AGENT_BASE_URL if agent else OPENCODE_BASE_URL
    headers = {"x-opencode-directory": racine}
    budget = OPENCODE_TIMEOUT if timeout is None else timeout
    # Fenêtre d'inactivité distincte du budget total : sans elle, une tâche dont
    # l'agent réfléchit trois minutes sans émettre apparaîtrait comme un serveur
    # muet alors qu'elle travaille. On garde une marge pour le pire cas.
    inactivite = min(float(budget), 300.0) if budget else 300.0
    try:
        async with httpx.AsyncClient(
            # Le flux SSE respecte le délai configuré : None laissait une requête
            # OpenCode bloquée indéfiniment sans nouvel événement.
            base_url=base.rstrip("/"), timeout=httpx.Timeout(inactivite, read=inactivite)
        ) as client:
            async with client.stream(
                "GET", "/event", params=params, headers=headers
            ) as stream:
                if stream.status_code >= 400:
                    raise ErreurOpenCode(
                        "serveur_indisponible",
                        f"Flux OpenCode indisponible ({stream.status_code}).",
                    )
                reponse = await client.post(
                    f"/session/{sid}/prompt_async", params=params,
                    headers=headers,
                    json={"parts": [{"type": "text", "text": texte}]},
                )
                if reponse.status_code >= 400:
                    raise ErreurOpenCode(
                        "serveur_indisponible",
                        f"OpenCode a refusé la tâche ({reponse.status_code}) : "
                        f"{reponse.text[:1000]}",
                    )
                lignes_data: list[str] = []
                async for ligne in stream.aiter_lines():
                    if ligne.startswith("data:"):
                        lignes_data.append(ligne[5:].strip())
                        continue
                    if ligne.strip() or not lignes_data:
                        continue
                    payload = _sse_payload("\n".join(lignes_data))
                    lignes_data = []
                    if payload is not None:
                        yield payload
    except httpx.ConnectError as exc:
        raise ErreurOpenCode("serveur_indisponible", f"Serveur OpenCode injoignable : {exc}") from exc
    except httpx.TimeoutException as exc:
        raise ErreurOpenCode("timeout", "Le flux OpenCode a expiré.") from exc


def repondre_permission(
    directory: str | Path, request_id: str, reply: str,
    message: str | None = None, agent: bool = False,
) -> dict[str, Any]:
    """Transmet la décision de l'utilisateur à OpenCode.

    `agent=False` par défaut : le serveur de discussion est celui qu'on atteint
    sans le dire. Un appelant qui cible le serveur dédié doit l'écrire
    explicitement, sinon il interroge le mauvais serveur et la demande reste
    bloquée — en silence, sans erreur visible côté interface.
    """
    if reply not in {"once", "always", "reject"}:
        raise ErreurOpenCode("schema_invalide", f"Réponse de permission inconnue : {reply}")
    racine, params = _serveur_params(directory)
    corps: dict[str, str] = {"reply": reply}
    if message:
        corps["message"] = message
    try:
        resp = _client(15, agent=agent).post(
            f"/permission/{request_id}/reply", params=params,
            headers={"x-opencode-directory": racine}, json=corps,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ErreurOpenCode("permission_indisponible", f"Permission OpenCode refusée ({exc.response.status_code}).") from exc
    except httpx.HTTPError as exc:
        raise ErreurOpenCode("serveur_indisponible", f"Réponse OpenCode impossible : {exc}") from exc
    return resp.json() if resp.content else {"ok": True}


def interrompre_session(
    directory: str | Path, sid: str, agent: bool = False
) -> None:
    """Interrompt une session.

    Même défaut prudent que `repondre_permission` : `agent=False`. Le serveur de
    discussion est celui qu'on atteint sans le dire. Un appelant qui cible le
    serveur dédié doit l'écrire explicitement — sinon il interroge le mauvais
    serveur et l'annulation ne fait rien, sans remonter d'erreur.
    """
    racine, params = _serveur_params(directory)
    try:
        _client(15, agent=agent).post(
            f"/session/{sid}/abort", params=params,
            headers={"x-opencode-directory": racine},
        )
    except httpx.HTTPError:
        logger.debug("Impossible d'interrompre la session OpenCode %s", sid, exc_info=True)
