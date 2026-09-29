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

from ..config import OPENCODE_BASE_URL, OPENCODE_CONFIG, OPENCODE_TIMEOUT

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


def _client(timeout: float = OPENCODE_TIMEOUT) -> httpx.Client:
    return httpx.Client(base_url=OPENCODE_BASE_URL.rstrip("/"), timeout=timeout)


def verifier_serveur() -> bool:
    try:
        return httpx.get(f"{OPENCODE_BASE_URL}/global/health", timeout=3).is_success
    except Exception:
        return False


def creer_session(directory: str | Path, title: str | None = None) -> str:
    """Crée une session OpenCode et retourne son id.

    Le contrat serveur est `POST /session { parentID?, title? }` : le system
    prompt N'EST PAS accepté ici. Il est transmis à chaque tour, dans le corps
    du message (cf. `envoyer_instruction`).
    """
    try:
        resp = _client(15).post(
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


def _serveur_binaire() -> str:
    """Résout le même binaire que le mode terminal."""
    from . import agent_tache
    return agent_tache.resoudre_binaire()


def _serveur_params(directory: str | Path) -> tuple[str, dict[str, str]]:
    racine = str(Path(directory).resolve())
    return racine, {"directory": racine}


def assurer_serveur(directory: str | Path) -> None:
    """Garantit un serveur OpenCode local pour le flux SSE."""
    global _SERVEUR_PROCESS
    if verifier_serveur():
        return
    if _SERVEUR_PROCESS is not None and _SERVEUR_PROCESS.poll() is not None:
        _SERVEUR_PROCESS = None
    cible = urlparse(OPENCODE_BASE_URL)
    hote = cible.hostname or "127.0.0.1"
    port = cible.port or 4096
    try:
        binaire = _serveur_binaire()
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        # La config de l'application porte les instructions et les permissions de
        # l'agent. Elle n'est pas auto-découverte : le `cwd` du serveur est le
        # projet de l'utilisateur, donc OpenCode lirait la config de CE projet.
        if OPENCODE_CONFIG.is_file():
            env["OPENCODE_CONFIG"] = str(OPENCODE_CONFIG.resolve())
        _SERVEUR_PROCESS = subprocess.Popen(
            [binaire, "serve", "--hostname", hote, "--port", str(port)],
            cwd=str(Path(directory).resolve()),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ErreurOpenCode) as exc:
        raise ErreurOpenCode(
            "moteur_indisponible", f"Impossible de lancer le serveur OpenCode : {exc}"
        ) from exc
    limite = time.monotonic() + 15
    while time.monotonic() < limite:
        if verifier_serveur():
            return
        if _SERVEUR_PROCESS is not None and _SERVEUR_PROCESS.poll() is not None:
            code = _SERVEUR_PROCESS.returncode
            _SERVEUR_PROCESS = None
            raise ErreurOpenCode(
                "moteur_indisponible",
                f"Le serveur OpenCode s'est arrêté pendant le démarrage (code {code}). " 
                "Sur Windows, EEXIST indique un bug de création du dossier de configuration OpenCode.",
            )
        time.sleep(0.25)
    raise ErreurOpenCode(
        "serveur_indisponible",
        f"Le serveur OpenCode ne répond pas sur {OPENCODE_BASE_URL}.",
    )


def arreter_serveur() -> None:
    """Arrête le serveur OpenCode lancé par CE processus (`opencode serve`).

    Sans cela, chaque arrêt/redémarrage du backend laisse un `opencode serve`
    orphelin qui garde le port 4096 occupé : le redémarrage suivant échoue
    silencieusement côté `assurer_serveur`. Un serveur déjà lancé par
    l'utilisateur (`_SERVEUR_PROCESS is None`) n'est jamais tué.
    """
    global _SERVEUR_PROCESS
    proc, _SERVEUR_PROCESS = _SERVEUR_PROCESS, None
    if proc is None or proc.poll() is not None:
        return
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
    directory: str | Path, sid: str, texte: str
) -> AsyncIterator[dict[str, Any]]:
    """Diffuse les événements OpenCode d'une tâche jusqu'à sa fin."""
    racine, params = _serveur_params(directory)
    headers = {"x-opencode-directory": racine}
    try:
        async with httpx.AsyncClient(
            # Le flux SSE respecte le délai configuré : None laissait une requête
            # OpenCode bloquée indéfiniment sans nouvel événement.
            base_url=OPENCODE_BASE_URL.rstrip("/"), timeout=OPENCODE_TIMEOUT
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
    directory: str | Path, request_id: str, reply: str, message: str | None = None
) -> dict[str, Any]:
    """Transmet réellement la décision de l'utilisateur à OpenCode."""
    if reply not in {"once", "always", "reject"}:
        raise ErreurOpenCode("schema_invalide", f"Réponse de permission inconnue : {reply}")
    racine, params = _serveur_params(directory)
    corps: dict[str, str] = {"reply": reply}
    if message:
        corps["message"] = message
    try:
        resp = _client(15).post(
            f"/permission/{request_id}/reply", params=params,
            headers={"x-opencode-directory": racine}, json=corps,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ErreurOpenCode("permission_indisponible", f"Permission OpenCode refusée ({exc.response.status_code}).") from exc
    except httpx.HTTPError as exc:
        raise ErreurOpenCode("serveur_indisponible", f"Réponse OpenCode impossible : {exc}") from exc
    return resp.json() if resp.content else {"ok": True}


def interrompre_session(directory: str | Path, sid: str) -> None:
    racine, params = _serveur_params(directory)
    try:
        _client(15).post(
            f"/session/{sid}/abort", params=params,
            headers={"x-opencode-directory": racine},
        )
    except httpx.HTTPError:
        logger.debug("Impossible d'interrompre la session OpenCode %s", sid, exc_info=True)
