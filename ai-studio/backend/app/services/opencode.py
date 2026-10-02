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
    "contexte fourni dans le message, où chaque ligne est précédée de son "
    "numéro. Ne réécris JAMAIS un fichier entier : réponds uniquement par les "
    "lignes qui changent, sous la forme d'une liste JSON "
    '[{"action": "patch", "fichier": "chemin/relatif.ext", "operations": '
    '[{"ligne": 42, "suppression": 1, "ajout": ["nouvelle ligne"]}]}]. '
    '"ligne" est le numéro affiché dans le contexte (il commence à 1), '
    '"suppression" le nombre de lignes existantes à remplacer, "ajout" les '
    'nouvelles lignes. Renvoie [] si rien ne doit changer, et jamais de texte '
    'autour du JSON.'
)

# Agent OpenCode du mode Chat, défini dans opencode.jsonc avec TOUS les outils en
# `deny`. Le nom doit correspondre exactement à la clé `agent.discussion` du
# fichier : une faute de frappe ferait retomber OpenCode sur l'agent par défaut
# (`build`), qui est autorisé à écrire et à exécuter des commandes.
CHAT_AGENT = "discussion"

# Agent du mode Edit. Distinct de CHAT_AGENT parce que les deux ne veulent pas la
# même chose : la discussion écrit des outils, le proposeur d'édition ne doit
# RIEN toucher. Les deux partagent le serveur 4096, donc héritent du
# `bash: "allow"` global. Sans agent dédié, l'Edit exécuterait des commandes
# alors que sa seule sortie attendue est une proposition de modification — et il
# perdrait du temps à explorer le projet avant de répondre.
EDIT_AGENT = "proposition"


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

    `agent` désigne l'agent `proposition` (cf. opencode.jsonc), dont tous les
    outils sont refusés. C'est la garantie STRUCTURELLE du contrat : une consigne
    dans le prompt peut être ignorée par le modèle, une permission non.
    """
    try:
        resp = _client().post(
            f"/session/{sid}/message",
            json={
                "system": SYSTEM_PROMPT,
                "agent": EDIT_AGENT,
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


def repondre_chat(directory: str | Path, prompt: str, timeout: float | None = None) -> str:
    """Un tour de discussion libre, sans aucun outil, et retourne le texte final.

    L'agent `discussion` (cf. opencode.jsonc) porte le contrat « aucun outil » :
    le Chat partage le serveur du mode Edit (4096), donc les permissions globales
    ne suffisent pas — laisser `bash` en `ask` ferait patienter une demande de
    permission que personne ne peut valider ici, et la réponse resterait bloquée.
    Le prompt de discussion contient DÉJÀ tout le contexte (arborescence, mémoire,
    fichiers) : l'agent n'a rien à lire ni à exécuter.
    """
    racine = str(Path(directory).resolve())
    Path(racine).mkdir(parents=True, exist_ok=True)
    assurer_serveur(racine)
    sid = creer_session(racine, "AI Studio · Chat", agent=False)
    budget = OPENCODE_TIMEOUT if timeout is None else timeout
    try:
        resp = _client(budget).post(
            f"/session/{sid}/message",
            json={
                "agent": CHAT_AGENT,
                "parts": [{"type": "text", "text": prompt}],
            },
            headers={"x-opencode-directory": racine},
        )
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        raise ErreurOpenCode(
            "serveur_indisponible", f"Serveur OpenCode injoignable : {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ErreurOpenCode(
            "timeout", f"OpenCode n'a pas répondu en {int(budget)} s."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ErreurOpenCode(
            "serveur_indisponible",
            f"Erreur {exc.response.status_code} lors de l'envoi.",
        ) from exc

    return _extraire_texte_final(resp.json())


# ─────────────────────── Exécution autonome headless ───────────────────────

_SERVEUR_PROCESS: subprocess.Popen | None = None
# Serveur du mode autonome, sur un port DIFFERENT. Deux processus distincts
# signifie deux environnements distincts : c'est la garantie structurelle que
# `AISTUDIO_AGENT` ne peut pas atteindre la session interactive de l'utilisateur.
_SERVEUR_AGENT_PROCESS: subprocess.Popen | None = None

# Date de modification de la config au moment où chaque serveur a été démarré.
# Sert au diagnostic : elle permet de constater qu'un serveur tourne avec une
# configuration abandonnée sans avoir à le deviner.
_CONFIG_MT_SERVEUR: dict[str, float] = {}
# Base déjà contrôlée dans CE process : inutile de re-tuer un serveur qu'on vient
# de démarrer soi-même.
_VETEE: set[str] = set()


def _mtime_config() -> float:
    try:
        return OPENCODE_CONFIG.stat().st_mtime
    except OSError:
        return 0.0


def _port_de(base: str) -> int:
    return urlparse(base).port or 4096


def _sortie(cmd: list[str]) -> str:
    """Lance une commande Windows et renvoie sa sortie, sans jamais échouer.

    `netstat` et `tasklist` n'écrivent pas en UTF-8 : sur une Windows française,
    le décodage cp1252 par défaut lève `UnicodeDecodeError` sur le moindre octet
    accentué, ce qui transformerait une simple détection de port en exception.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _pid_ecoutant(port: int) -> int | None:
    """PID du processus qui écoute `port`, ou None. Windows uniquement.

    L'état d'une connexion est traduit par Windows (« LISTENING » en anglais,
    « EN ÉCOUTE » en français) : le comparer à une chaîneanglaise marche sur une
    machine anglophone et jamais sur une autre. On ne teste donc pas l'état, mais
    l'adresse distante, qui vaut `0.0.0.0:0` pour une socket en écoute — un fait
    du format, pas de la localisation.
    """
    if os.name != "nt":
        return None
    cible = f":{port}"
    for ligne in _sortie(["netstat", "-ano", "-p", "TCP"]).splitlines():
        champs = ligne.split()
        if len(champs) < 5 or champs[0].upper() != "TCP":
            continue
        if champs[1].endswith(cible) and champs[2] == "0.0.0.0:0":
            try:
                return int(champs[4])
            except ValueError:
                return None
    return None


def _est_opencode(pid: int) -> bool:
    """Vrai si `pid` est bien un serveur OpenCode.

    Garde-fou indispensable : `_renouveler_serveur` ne doit jamais arrêter un
    processus qui a juste le malheur d'occuper le port — un port occupé par
    autre chose est un conflit de port, pas un serveur périmé.
    """
    if os.name != "nt":
        return False
    return "opencode" in _sortie(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    ).lower()


def _config_du_serveur(base: str) -> dict[str, Any] | None:
    """Configuration que le serveur en face a réellement chargée, ou None.

    `GET /config` répond la configuration effective, config d'environnement
    comprise. C'est la seule preuve fiable : un serveur déjà lancé peut venir de
    n'importe quelle configuration, y compris celle de l'utilisateur.
    """
    try:
        rep = httpx.get(f"{base}/config", timeout=5)
        rep.raise_for_status()
        donnees = rep.json()
    except (httpx.HTTPError, ValueError):
        return None
    return donnees if isinstance(donnees, dict) else None


def _tourne_avec_notre_config(base: str) -> bool:
    """Vrai si le serveur a chargé la configuration d'AI Studio, À JOUR.

    Les agents `discussion` et `proposition` n'existent que dans notre
    `opencode.jsonc`. Les retrouver prouve que le serveur a bien lu NOTRE
    fichier, donc qu'il s'agit d'un serveur à nous — un démarrage précédent de
    l'application — et non d'un `opencode serve` de l'utilisateur.

    Exiger les DEUX est ce qui rend la détection d'une mise à jour : une
    installation antérieure a un serveur qui connaît `discussion` mais ignore
    `proposition`, ajouté plus tard. Sans cette exigence, l'Edit tomberait en
    silence sur l'agent par défaut, qui a le droit d'écrire et d'exécuter.

    À ne PAS confondre avec « il applique les permissions du fichier » : ce
    n'est pas vrai. Le serveur retient sa configuration au DÉMARRAGE.
    """
    config = _config_du_serveur(base)
    if config is None:
        return False
    agents = config.get("agent")
    if not isinstance(agents, dict):
        return False
    return all(nom in agents for nom in (CHAT_AGENT, EDIT_AGENT))


def _renouveler_serveur(base: str) -> bool:
    """Force le serveur de `base` à recharger la configuration d'AI Studio.

    Un OpenCode lit sa configuration au DÉMARRAGE et la garde en mémoire : un
    serveur déjà en écoute sert donc les permissions qu'il avait au moment où il
    a démarré, pas celles du fichier. Constaté ici : le serveur répondait
    `"bash": "ask"` alors que opencode.jsonc disait `"allow"`. Corriger le
    fichier ne changeait donc rien, et l'agent Edit restait bloqué sur une
    permission que l'utilisateur croyait avoir levée.

    Seuls les serveurs À NOUS sont arrêtés, reconnaissables à leur agent
    `discussion`. Un `opencode serve` lancé par l'utilisateur pour sa propre
    session n'est jamais touché : `_renouveler_serveur` renvoie alors False et
    l'appelant se contente de l'utiliser.
    """
    port = _port_de(base)
    if not _sante(base) or not _tourne_avec_notre_config(base):
        return False
    pid = _pid_ecoutant(port)
    if pid is None or not _est_opencode(pid):
        return False
    logger.warning(
        "serveur_perime port=%s pid=%s — redemarrage pour recharger la config", port, pid
    )
    _sortie(["taskkill", "/PID", str(pid), "/F"])
    limite = time.monotonic() + 15
    while time.monotonic() < limite:
        if not _sante(base):
            return True
        time.sleep(0.25)
    return False


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
    """Serveur du mode discussion (port 4096), sans aucun flag agent.

    Le contrôle de configuration a lieu UNE FOIS par process (`_VETEE`) : le
    serveur étant relancé à ce moment-là, il sert la config courante. Les
    appels suivants — un par question au chat — réutilisent ce serveur sans
    repasser par un `/config`.
    """
    global _SERVEUR_PROCESS
    if verifier_serveur():
        if OPENCODE_BASE_URL in _VETEE:
            return
        _VETEE.add(OPENCODE_BASE_URL)
        if not _renouveler_serveur(OPENCODE_BASE_URL):
            # Serveur de l'utilisateur, ou port pris par autre chose : on ne le
            # détruit pas, on tente de s'en servir.
            return
    if _SERVEUR_PROCESS is not None and _SERVEUR_PROCESS.poll() is not None:
        _SERVEUR_PROCESS = None
    _SERVEUR_PROCESS = _demarrer_serveur(directory, OPENCODE_BASE_URL, agent=False)
    _CONFIG_MT_SERVEUR[OPENCODE_BASE_URL] = _mtime_config()


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
    _VETEE.discard(OPENCODE_AGENT_BASE_URL)
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
    _CONFIG_MT_SERVEUR.clear()
    _VETEE.clear()
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
