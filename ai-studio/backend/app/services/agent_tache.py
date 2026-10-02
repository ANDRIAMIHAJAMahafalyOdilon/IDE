"""Moteur de tâches autonomes : l'agent OpenCode EXÉCUTE réellement la consigne.

Le chapitre 1 de l'architecture (proposeur seul) garde l'agent en lecture seule
et renvoie des propositions validées par l'utilisateur. Le MODE AUTONOME inverse
le contrat : « opencode run --format json --auto --dir <projet> <consigne> »
permet au vrai agent de lire, écrire et exécuter des commandes — tout reste
borné au dossier du projet (cwd du processus).

Sortie consommée : `--format json` (opencode ≥1.18) émet sur stdout des lignes
JSON d'événements au niveau racine (`type`, `sessionID`, `part`) :
    {"type":"step_start", "sessionID":"ses_...", "part":{...}}
    {"type":"tool_use",  "sessionID":"ses_...", "part":{"type":"tool",
        "tool":"write", "state":{"input":{"filePath":...}, "output":...}}}
    {"type":"text",      "sessionID":"ses_...",
        "part":{"type":"text", "text":"..."}}
    {"type":"step_finish","sessionID":"ses_...", "part":{...}}
`mapper_ligne` traduit chaque ligne en événements d'UI :
    ('debut',  {session, moteur})
    ('texte',  {delta})        — texte accumulé du modèle, résidu par partie
    ('outil',  {type, fichier?, commande?, resume?}) — edit/write/bash/etc.
    ('fin',    {session})      — agent terminé (code 0)
    ('erreur', {code, message})— process échoué / timeout / binaire absent

Testabilité : `executer_tache` accepte un objet *processus* injecté ; les tests
injectent un faux `stdout` (readline successif) et contrôlent returncode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Any

from ..config import (
    OPENCODE_BIN,
    OPENCODE_CONFIG,
    OPENCODE_MODEL,
    TACHE_TIMEOUT,
    OUTIL_APERCU_MAX,
    OUTIL_RESUME_MAX,
)

logger = logging.getLogger(__name__)

# Les codes couleur ANSI qu'OpenCode écrit sur stderr : sans ce retrait, le
# diagnostic affiché à l'utilisateur serait illisible.
_SANS_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Clé d'état du premier texte d'une étape, en attente de verdict.
#
# En mode CLI, OpenCode émet l'intention comme un `text` ordinaire : aucune
# distinction structurelle ne permet de la séparer d'une vraie réponse. Filtrer
# sur des motifs (« I'll… », « Let me… ») serait fragile et supprimerait des
# réponses légitimes commençant par « D'abord… ». On diffère donc le texte, et
# c'est l'événement suivant qui tranche : un outil suit -> c'était une annonce,
# un texte ou la fin de l'étape -> c'était la réponse.
CLE_NARRATION = "__narration__"
CLE_ARME = "__narration_armee__"

def _binaire_npm_global() -> str | None:
    """Cherche le binaire OpenCode dans le dossier global npm (portable, sans chemin en dur).

    Lit `npm prefix -g` pour trouver le dossier d'installation global de npm,
    puis regarde `opencode-ai/bin/opencode.exe` (Windows) et
    `opencode-ai/bin/opencode` (Linux/macOS : le script `postinstall` du paquet
    y dépose le binaire natif de la plateforme). Retourne None si npm est
    introuvable ou si aucun binaire n'existe à cet emplacement.
    """
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        resultat = subprocess.run(
            [npm, "prefix", "-g"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        prefix = resultat.stdout.strip()
        if not prefix:
            return None
        base = Path(prefix) / "node_modules" / "opencode-ai" / "bin"
        for nom in ("opencode.exe", "opencode"):
            candidat = base / nom
            if candidat.exists():
                return str(candidat)
        return None
    except Exception:  # noqa: BLE001
        return None


class ErreurTache(Exception):
    """Erreur métier du mode autonome (code normalisé pour l'événement `erreur`)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ─────────────────── Résolution du binaire OpenCode ───────────────────────

def resoudre_binaire() -> str:
    """Chemins du binaire CLI : override config, npm global, shim PATH, sinon erreur."""
    if OPENCODE_BIN:
        if not Path(OPENCODE_BIN).exists():
            raise ErreurTache(
                "moteur_indisponible",
                f"OPENCODE_BIN configuré mais introuvable : {OPENCODE_BIN!r}",
            )
        return OPENCODE_BIN
    # 1) Résolution portable via `npm prefix -g` (aucun chemin utilisateur en dur).
    npm_global = _binaire_npm_global()
    if npm_global:
        return npm_global
    TROUVE = shutil.which("opencode")
    suffixe = Path(TROUVE).suffix.lower() if TROUVE else ""
    # POSIX : `which` renvoie déjà le binaire natif, sans extension ni shim.
    if TROUVE and suffixe == ".exe":
        return TROUVE
    if TROUVE and os.name != "nt":
        return TROUVE
    # Shim .cmd/.ps1 npm : on relit le contenu et on extrait le .exe réel.
    if TROUVE:
        chemin = Path(TROUVE)
        if chemin.suffix.lower() in {".cmd", ".ps1", ""}:
            try:
                for ligne in chemin.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "node_modules\\opencode-ai\\bin\\opencode.exe" in ligne:
                        exe = ligne.strip("\"") .split('%*')[0].strip('"').strip()
                        exe = exe.replace('%dp0%', str(chemin.parent))
                        if Path(exe).exists():
                            return exe
            except OSError:
                pass
    raise ErreurTache(
        "moteur_indisponible",
        "Binaire OpenCode introuvable. Installe la CLI (`npm i -g opencode-ai`) "
        "ou définit OPENCODE_BIN.",
    )


# ─────────────────────── Traduction des lignes JSON ────────────────────────

def _sid_ligne(evt: dict) -> str | None:
    """Session OpenCode (ID COMPLET, utilisé tel quel pour le fork suivant)."""
    sid = evt.get("sessionID") or evt.get("sessionId")
    if not sid:
        part = evt.get("part") or {}
        sid = part.get("sessionID")
    if not sid:
        props = evt.get("properties") or {}
        sid = props.get("sessionID") or props.get("sessionId")
    return str(sid) if sid else None


def _resume_part(part: dict) -> str:
    """Résumé lisible d'un part d'outil (output) pour la pastille `outil`."""
    etat = part.get("state") or {}
    resume = ""
    for cle in ("last_output", "output"):
        val = etat.get(cle)
        if not val:
            continue
        if isinstance(val, str):
            resume = val
        elif isinstance(val, dict):
            resume = val.get("__text") or val.get("content") or ""
        if resume:
            break
    if not resume:
        resume = etat.get("status") or ""
    return str(resume).replace("\r", "")[:OUTIL_RESUME_MAX].strip()


def _classer_erreur_cli(erreur: Any) -> dict[str, Any]:
    """Traduit l'erreur d'un fournisseur en code et message pour l'UI."""
    detail = ""
    statut: Any = None
    if isinstance(erreur, dict):
        data = erreur.get("data")
        if isinstance(data, dict):
            detail = str(data.get("message") or "")
            statut = data.get("statusCode")
        else:
            detail = str(erreur.get("message") or "")
    elif erreur is not None:
        detail = str(erreur)

    texte = detail.lower()
    try:
        code_statut = int(statut) if statut is not None else None
    except (TypeError, ValueError):
        code_statut = None

    # Testé AVANT le cas 401/403 : la réponse est bien un 403, mais la clé est
    # acceptée — c'est le palier gratuit qui interdit le mode ligne de commande.
    # Classé en « auth », l'utilisateur croyait sa clé rejetée et la corrigeait à
    # tort ; en réalité aucune clé ne Solutionne tant qu'OpenCode n'a pas rouvert
    # le CLI.
    if "free tier" in texte or "frettierror" in texte or "within opencode" in texte:
        return {
            "code": "palier_gratuit",
            "message": "OpenCode refuse le mode ligne de commande sur le palier "
                       "gratuit (erreur « free tier » du fournisseur). La clé est "
                       "valide : il faut un compte payant, ou attendre la "
                       "réouverture du CLI.",
        }
    if "quota" in texte or "rate limit" in texte or "resource_exhausted" in texte \
            or code_statut == 429:
        return {
            "code": "quota",
            "message": "Quota du fournisseur dépassé. Réessaie dans un instant.",
        }
    if code_statut in (401, 403) or "api key" in texte or "permission" in texte:
        return {"code": "auth", "message": "Le fournisseur a refusé la clé API."}
    if code_statut == 404 or "no longer available" in texte:
        return {
            "code": "moteur_indisponible",
            "message": "Le modèle demandé n'est plus disponible. "
                       "Changez OPENCODE_MODEL dans le .env.",
        }
    return {
        "code": "interne",
        "message": detail[:300] or "L'agent a signalé une erreur.",
    }


def mapper_ligne(
    ligne: str,
    textes: dict[str, str],
    racine: Path | None = None,
    activites: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Traduit une ligne d'événement JSON opencode en événements d'UI.

    Maintient l'état d'accumulation du texte par part id (passé via *textes*).
    *racine* (optionnel) rend les chemins d'outils relatifs au projet.
    *activites* conserve l'étape en cours entre deux lignes JSON.
    Retourne [] si la ligne n'est pas un événement exploitable.
    """
    ligne = ligne.strip()
    if not ligne:
        return []
    try:
        evt = json.loads(ligne)
    except json.JSONDecodeError:
        return []
    if not isinstance(evt, dict):
        return []

    genre = evt.get("type")
    props = evt.get("properties") or {}
    part = evt.get("part") or props.get("part") or {}
    sorties: list[tuple[str, dict[str, Any]]] = []
    etat_activites = activites if activites is not None else {}

    if not isinstance(part, dict):
        part = {}

    if genre == "error":
        # `opencode run` rapporte l'échec du fournisseur comme un événement
        # JSON de premier niveau. Sans ce cas, la ligne était ignorée comme
        # n'importe quel événement inconnu et l'utilisateur ne recevait que
        # « l'agent s'est arrêté avec le code 1 », sans cause.
        sorties.append(("erreur", _classer_erreur_cli(evt.get("error"))))
        return sorties

    if genre == "text" and part.get("type") == "text":
        texte = str(part.get("text") or "")
        pid = str(part.get("id") or "main")
        precedent = textes.get(pid, "")
        if len(texte) > len(precedent):
            delta = texte[len(precedent):]
            textes[pid] = texte
            if delta:
                if not etat_activites.get(CLE_ARME):
                    # L'étape a déjà agi : ce texte est un résultat, il part.
                    sorties.append(("texte", {"delta": delta}))
                elif CLE_NARRATION in etat_activites:
                    # Deuxième texte avant toute action : le premier était la
                    # réponse, il n'était pas une annonce.
                    attente = etat_activites.pop(CLE_NARRATION)
                    sorties.append(("texte", {"delta": str(attente.get("texte") or "")}))
                    sorties.append(("texte", {"delta": delta}))
                else:
                    # Premier texte de l'étape : nature encore inconnue.
                    etat_activites[CLE_NARRATION] = {"pid": pid, "texte": texte}
        return sorties

    if genre == "tool_use" and part.get("type") == "tool":
        # Un texte d'annonce précède toujours l'action qu'il annonce : c'est du
        # raisonnement destiné au modèle, pas une réponse pour l'utilisateur.
        # On l'écarte, et on purge son accumulateur pour qu'un réémargement
        # ultérieur reparte de zéro. Tout texte suivant est un résultat.
        narration = etat_activites.pop(CLE_NARRATION, None)
        if isinstance(narration, dict):
            textes.pop(str(narration.get("pid") or ""), None)
        etat_activites[CLE_ARME] = False
        call_id = str(part.get("callID") or part.get("id") or evt.get("id") or "outil")
        outil = str(part.get("tool") or "")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        saisie = state.get("input") or part.get("input") or {}
        info = _outil_base(call_id, outil, saisie, racine or Path.cwd())
        statut = str(state.get("status") or "running")
        info["status"] = {
            "pending": "pending",
            "running": "running",
            "completed": "success",
            "success": "success",
            "error": "error",
            "failed": "error",
        }.get(statut, "running")
        resume = _resume_part(part)
        if resume:
            info["output"] = resume
            # Alias conservé pour les anciens consommateurs du flux CLI.
            info["resume"] = resume
        if state.get("title"):
            info["description"] = str(state["title"])
        etat_activites[call_id] = info
        sorties.append(("activite", info))
        return sorties

    if genre in {"step_start", "step.start", "step_finish", "step.finish"}:
        sid = _sid_ligne(evt) or "tache"
        termine = genre in {"step_finish", "step.finish"}
        # Aucune étape ne peut hériter de l'attente de la précédente.
        attente = etat_activites.pop(CLE_NARRATION, None)
        etat_activites[CLE_ARME] = not termine
        if termine and isinstance(attente, dict) and str(attente.get("texte") or "").strip():
            # L'étape s'achève sans outil derrière le texte : c'était bien la
            # réponse, elle part finalement — un peu plus tard, jamais ratée.
            sorties.append(("texte", {"delta": str(attente.get("texte") or "")}))
        if termine:
            ident = str(etat_activites.pop("__step_courant__", f"step:{sid}"))
            sorties.append(("activite", {
                "id": ident,
                "type": "thinking",
                "status": "success",
                "title": "Étape terminée",
                "description": str((part.get("reason") or "OpenCode passe à la suite.")).strip(),
            }))
            return sorties
        compteur = int(etat_activites.get("__nb_etapes__", 0)) + 1
        etat_activites["__nb_etapes__"] = compteur
        # Un identifiant PAR ÉTAPE. Avec un identifiant unique et réutilisé
        # (`step:...:current`), le front déduplique par id : toutes les étapes
        # fusionnaient en une seule carte « Analyse du projet » écrasée en
        # place, et l'historique réel disparaissait. Le `:current` mémorise
        # justement quelle étape est ouverte, pour qu'elle se referme sur son
        # propre `step_finish` au lieu de clore celle d'avant.
        ident = f"step:{sid}:{compteur}"
        etat_activites["__step_courant__"] = ident
        return [("activite", {
            "id": ident,
            "type": "thinking",
            "status": "running",
            "title": "Analyse du projet",
            "description": "OpenCode lit le contexte et prépare sa prochaine action…",
        })]

    if genre == "part.updated":
        return []

    # Événements inconnus / progression qu'on ne consomme pas.
    return []


# ────────────────────────── Exécution réelle (générateur) ──────────────────

def _commande(binaire: str, racine: Path, message: str, sid: str | None) -> list[str]:
    cmd = [
        binaire,
        "run",
        "--format",
        "json",
        "--auto",
        "--dir",
        str(racine),
    ]
    if OPENCODE_MODEL:
        cmd += ["--model", OPENCODE_MODEL]
    if sid:
        # Reprendre la même session conserve le contexte et évite de cloner
        # tout le fil à chaque requête. Le fork provoquait des relances et une
        # perte de continuité visibles dans Edit.
        cmd += ["-s", sid]
    cmd.append(message)
    return cmd


def _demarrer(binaire: str, racine: Path, message: str, sid: str | None) -> subprocess.Popen:
    cmd = _commande(binaire, racine, message, sid)
    logger.info("tache agent : %s", " ".join(cmd))
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    # `opencode run` part avec `--dir` sur le projet de l'utilisateur : sans cette
    # variable il y découvre SON opencode.jsonc et applique ses permissions, pas
    # les nôtres. C'est ce mode qui est réellement utilisé par l'API, donc c'est
    # ici, et non dans le chemin serveur, que la config doit être transmise.
    env["OPENCODE_CONFIG"] = str(OPENCODE_CONFIG)
    # Marqueur lu par le plugin `aistudio-arriere-plan.js` : sans lui, une
    # commande serveur passe au premier plan, y compris dans la session
    # interactive de l'utilisateur où c'est le comportement voulu.
    env["AISTUDIO_AGENT"] = "1"
    # Lus par le plugin pour réécrire la commande ET pour écrire son journal
    # hors des projets de l'utilisateur (un `.log` dans son dépôt le salirait).
    try:
        from .agent_fond import chemin_lanceur, dossier_agents

        env["AISTUDIO_LANCEUR"] = str(chemin_lanceur())
        env["AISTUDIO_LOG_DIR"] = str(dossier_agents())
    except Exception:  # noqa: BLE001 — sans lanceur, le plugin ne réécrit rien
        logger.debug("Lancement arriere-plan indisponible", exc_info=True)
    # Le fournisseur Google d'OpenCode ne lit que GOOGLE_GENERATIVE_AI_API_KEY,
    # alors que l'application s'appelle GEMINI_API_KEY. Sans cet alias, OpenCode
    # démarre sans credential et répond « pas de clé » alors que la clé est
    # présente dans le .env. Alias dans l'environnement de l'enfant, jamais dans
    # le .env : on ne duplique pas le secret.
    if not env.get("GOOGLE_GENERATIVE_AI_API_KEY") and env.get("GEMINI_API_KEY"):
        env["GOOGLE_GENERATIVE_AI_API_KEY"] = env["GEMINI_API_KEY"]
    return subprocess.Popen(
        cmd,
        cwd=racine,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ─────────────────── Traduction des événements serveur ─────────────────────


def _chemin_relatif(chemin: Any, racine: Path) -> str | None:
    if not chemin or not isinstance(chemin, str):
        return None
    try:
        return Path(chemin).resolve().relative_to(racine.resolve()).as_posix()
    except ValueError:
        return chemin


def _sortie(valeur: Any) -> str:
    if valeur is None:
        return ""
    if isinstance(valeur, str):
        return valeur
    if isinstance(valeur, dict):
        for cle in ("output", "content", "message", "text"):
            if cle in valeur:
                return _sortie(valeur[cle])
    if isinstance(valeur, list):
        return "\n".join(_sortie(x) for x in valeur)
    return str(valeur)


def _apercu_modification(outil: str, saisie: dict[str, Any], fichier: str | None) -> str:
    """Construit un aperçu diff à partir des entrées des outils OpenCode."""
    patch = saisie.get("patch") or saisie.get("diff")
    if patch:
        return str(patch)[:OUTIL_APERCU_MAX]

    ancien = saisie.get("oldString") or saisie.get("old_string") or saisie.get("before")
    nouveau = saisie.get("newString") or saisie.get("new_string") or saisie.get("after")
    if ancien is not None or nouveau is not None:
        nom = fichier or "fichier"
        lignes = [f"--- {nom}", f"+++ {nom}"]
        lignes.extend(f"- {ligne}" for ligne in str(ancien or "").splitlines())
        lignes.extend(f"+ {ligne}" for ligne in str(nouveau or "").splitlines())
        return "\n".join(lignes)[:OUTIL_APERCU_MAX]

    if outil in {"write", "create"}:
        contenu = saisie.get("content") or saisie.get("contents") or saisie.get("text")
        if contenu is not None:
            nom = fichier or "nouveau fichier"
            lignes = [f"+++ {nom}"]
            lignes.extend(f"+ {ligne}" for ligne in str(contenu).splitlines())
            return "\n".join(lignes)[:OUTIL_APERCU_MAX]
    return ""


def _outil_base(call_id: str, outil: str, saisie: Any, racine: Path) -> dict[str, Any]:
    saisie = saisie if isinstance(saisie, dict) else {}
    chemin_brut = saisie.get("filePath") or saisie.get("path") or saisie.get("file")
    chemin = _chemin_relatif(chemin_brut, racine)
    commande = saisie.get("command") or saisie.get("cmdline")
    if outil in {"read", "cat"}:
        genre, titre = "file_read", "Lecture"
    elif outil in {"grep", "glob", "list", "search"}:
        genre, titre = "file_search", "Recherche"
    elif outil in {"edit", "apply_patch", "patch"}:
        genre, titre = "file_write", "Modification"
    elif outil in {"delete", "remove"}:
        genre, titre = "file_delete", "Suppression"
    elif outil in {"write", "create"}:
        chemin_existe = Path(str(chemin_brut)) if chemin_brut else Path()
        if chemin_existe and not chemin_existe.is_absolute():
            chemin_existe = racine / chemin_existe
        existe = bool(chemin_brut and chemin_existe.exists())
        genre, titre = ("file_write", "Modification") if existe else ("file_create", "Création")
    elif outil in {"bash", "shell", "terminal"}:
        genre, titre = "terminal", "Commande"
    else:
        genre, titre = "tool", outil or "Outil"
    info: dict[str, Any] = {"id": call_id, "type": genre, "status": "running", "title": titre}
    if chemin:
        info["fichier"] = chemin
    if commande:
        info["commande"] = str(commande)
    if outil in {"grep", "glob"} and (saisie.get("pattern") or saisie.get("query")):
        info["description"] = str(saisie.get("pattern") or saisie.get("query"))
    apercu = _apercu_modification(outil, saisie, chemin)
    if apercu:
        info["diff"] = apercu
    if genre == "terminal":
        info["environnement"] = "local"
        info["raison"] = "OpenCode exécute cette commande dans le projet."
    return info


def _texte_ou_narration(
    delta: str,
    cle: str,
    textes: dict[str, Any],
    etat_activites: dict[str, Any],
    sorties: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Écarte le texte d'annonce qui précède une action, comme le mapper CLI.

    Un texte qui précède un outil est du raisonnement destiné au modèle : l'afficher
    ferait croire à l'utilisateur que l'agent a déjà commencé. Mais le texte ne peut
    être jugé qu'après coup, d'où l'attente d'un second texte ou d'un outil.

    Clé de dé-doublonnage : la dernière réponse complète déjà diffusée. Sans elle, le
    mécanisme se retourne contre lui — un modèle qui réécrit sa réponse (au lieu de
    l'allonger) verrait sa VRAIE réponse être jetée.
    """
    if not delta:
        return sorties
    # Rien n'a encore été armé dans cette étape : le texte est une réponse.
    if not etat_activites.get(CLE_ARME):
        etat_activites.pop(CLE_NARRATION, None)
        return [*sorties, ("texte", {"delta": delta})]
    if CLE_NARRATION in etat_activites:
        # Deuxième texte avant toute action : le premier était la réponse.
        attente = etat_activites.pop(CLE_NARRATION)
        return [*sorties,
                ("texte", {"delta": str(attente.get("delta") or "")}),
                ("texte", {"delta": delta})]
    # Premier texte de l'étape : sa nature n'est pas encore connue.
    etat_activites[CLE_NARRATION] = {"cle": cle, "delta": delta}
    return sorties


def _purger_narration(etat_activites: dict[str, Any], arme: bool) -> None:
    """Une action commence : le texte en attente était une annonce.

    `arme=False` après un outil : le texte suivant est alors un RÉSULTAT. Sans
    ce réglage, la vraie réponse d'après un outil resterait en attente et ne
    partirait jamais — l'utilisateur verrait l'agent se taire après avoir agi.
    """
    etat_activites.pop(CLE_NARRATION, None)
    etat_activites[CLE_ARME] = arme


def mapper_evenement_serveur(
    evt: dict[str, Any], racine: Path, outils: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Normalise les événements OpenCode réels sans inventer d'activité."""
    genre = str(evt.get("type") or "")
    props = evt.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    sid = props.get("sessionID") or evt.get("sessionID")
    roles = outils.setdefault("__roles__", {})
    textes = outils.setdefault("__textes__", {})
    # L'état du raisonnement est porté par `outils`, le seul objet partagé entre
    # les appels (le paramètre `activites` du mapper CLI n'existe pas ici).
    etat_activites = outils.setdefault("__activites__", {})
    sorties: list[tuple[str, dict[str, Any]]] = []

    if genre == "message.updated":
        info = props.get("info")
        if isinstance(info, dict) and info.get("id"):
            roles[str(info["id"])] = str(info.get("role") or "")
            model_id = str(info.get("modelID") or "")
            if info.get("role") == "assistant" and model_id:
                return [("activite", {
                    "id": f"session:{sid}:status",
                    "type": "thinking",
                    "status": "running",
                    "title": f"Analyse OpenCode · {model_id}",
                    "description": f"Modèle actif : {model_id}",
                })]
        return sorties

    if genre == "message.part.delta":
        message_id = str(props.get("messageID") or "")
        if roles.get(message_id) != "assistant":
            return sorties
        delta = str(props.get("delta") or "")
        champ = str(props.get("field") or "text")
        if not delta:
            return sorties
        if champ == "reasoning":
            etat_activites[CLE_ARME] = True
            return [("activite", {
                "id": message_id + ":reasoning",
                "type": "thinking",
                "status": "running",
                "title": "Raisonnement OpenCode",
                "description": delta,
            })]
        # Le texte déjà diffusé est mémorisé ICI, sous la clé de la partie ET
        # sous celle du message. Sans cela, le gestionnaire `message.part.updated`
        # qui reçoit l'instantané complet rejouait tout le texte une seconde
        # fois : le backend renvoyait deux fois chaque réponse.
        cle = str(props.get("partID") or props.get("partId") or message_id)
        for k in {cle, message_id}:
            if len(textes.get(k, "")) < len(textes.get(cle, "")) + len(delta):
                textes[k] = textes.get(cle, "") + delta
        return _texte_ou_narration(delta, cle, textes, etat_activites, sorties)

    if genre == "message.part.updated":
        part = props.get("part")
        if not isinstance(part, dict):
            return sorties
        message_id = str(part.get("messageID") or "")
        part_type = str(part.get("type") or "")

        if part_type == "text" and roles.get(message_id) == "assistant":
            texte = str(part.get("text") or "")
            # `message.part.delta` peut avoir mémorisé l'état sous la clé de la
            # partie ou sous celle du message selon la forme de l'événement. On
            # retient la plus longue des deux, sinon l'instantané complet rejouait
            # du texte déjà diffusé. Même règle de croissance que `mapper_ligne` :
            # si le texte a été réécrit plutôt qu'allongé, on n'émet rien plutôt
            # que de dupliquer la réponse dans le panneau.
            cle = str(part.get("id") or message_id)
            ancien = max((textes.get(cle, ""), textes.get(message_id, "")), key=len)
            if len(texte) > len(ancien):
                textes[cle] = texte
                if message_id:
                    textes[message_id] = texte
            delta = texte[len(ancien):] if texte.startswith(ancien) else ""
            if not delta:
                return sorties
            return _texte_ou_narration(delta, cle, textes, etat_activites, sorties)

        if part_type in {"reasoning", "thinking"}:
            texte = str(part.get("text") or "")
            ident = str(part.get("id") or message_id or "raisonnement")
            ancien = str(textes.get(ident) or "")
            delta = texte[len(ancien):] if texte.startswith(ancien) else texte
            textes[ident] = texte
            etat_activites[CLE_ARME] = True
            return [("activite", {
                "id": ident,
                "type": "thinking",
                "status": "running",
                "title": "Raisonnement OpenCode",
                "description": delta,
            })] if delta else sorties

        if part_type == "tool":
            _purger_narration(etat_activites, False)
            call_id = str(part.get("callID") or part.get("id") or evt.get("id") or "outil")
            outil = str(part.get("tool") or "outil")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            saisie = state.get("input") or part.get("input")
            info = _outil_base(call_id, outil, saisie, racine)
            statut = str(state.get("status") or "running")
            info["status"] = {
                "pending": "pending",
                "running": "running",
                "completed": "success",
                "success": "success",
                "error": "error",
                "failed": "error",
            }.get(statut, "running")
            contenu = _sortie(state.get("output") or state.get("result"))
            if contenu:
                info["output"] = contenu[:OUTIL_RESUME_MAX]
            erreur = _sortie(state.get("error"))
            if erreur:
                info["error"] = erreur[:OUTIL_RESUME_MAX]
            if state.get("title"):
                info["description"] = str(state["title"])
            outils[call_id] = info
            return [("activite", info)]

    if genre == "session.next.text.delta":
        delta = str(props.get("delta") or "")
        return [("texte", {"delta": delta})] if delta else sorties
    if genre == "session.next.reasoning.delta":
        delta = str(props.get("delta") or "")
        return [("activite", {
            "id": str(props.get("textID") or evt.get("id") or "raisonnement"),
            "type": "thinking",
            "status": "running",
            "title": "Raisonnement OpenCode",
            "description": delta,
        })] if delta else sorties
    if genre in {"session.next.step.started", "step_start"}:
        # Une nouvelle étape arme la détection : ce qui compte comme réponse est
        # réarmé. Sans cela, le texte d'annonce de l'étape précédente resterait
        # armé et ferait passer la vraie réponse de la suivante pour une annonce.
        _purger_narration(etat_activites, True)
        # Identifiant unique par étape, voir le mapper ligne ~351 : un id
        # réutilisé fusionnerait toute la timeline en une seule carte.
        compteur = int(etat_activites.get("__nb_etapes__", 0)) + 1
        etat_activites["__nb_etapes__"] = compteur
        ident = f"step:{sid}:{compteur}"
        etat_activites["__step_courant__"] = ident
        return [("activite", {
            "id": ident, "type": "thinking", "status": "running",
            "title": "Analyse du projet",
        })]
    if genre in {"session.next.step.ended", "step_finish"}:
        # Filet de sécurité, comme côté CLI : une étape qui s'achève sans outil
        # derrière le texte, c'était bien la réponse. Elle part finalement, un peu
        # plus tard, plutôt que d'être perdue.
        attente = etat_activites.pop(CLE_NARRATION, None)
        etat_activites[CLE_ARME] = False
        if isinstance(attente, dict) and str(attente.get("delta") or "").strip():
            sorties.append(("texte", {"delta": str(attente.get("delta"))}))
        ident = str(etat_activites.pop("__step_courant__", f"step:{sid}:1"))
        return [("activite", {
            "id": ident, "type": "thinking", "status": "success",
            "title": "Analyse terminée",
        })]
    if genre == "session.next.tool.input.started":
        _purger_narration(etat_activites, False)
        call_id = str(props.get("callID") or evt.get("id") or "outil")
        outil = str(props.get("tool") or "outil")
        info = _outil_base(call_id, outil, props.get("input"), racine)
        info["status"] = "pending"
        outils[call_id] = info
        return [("activite", info.copy())]
    if genre in {"session.next.retry", "session.next.retried"}:
        return [("activite", {
            "id": str(evt.get("id") or "retry"),
            "type": "thinking",
            "status": "running",
            "title": "Nouvelle tentative",
            "description": _sortie(props.get("error")),
        })]
    if genre in {"session.next.tool.called", "tool_use"}:
        saisie = props.get("input")
        if not saisie and isinstance(evt.get("part"), dict):
            part = evt["part"]
            saisie = (part.get("state") or {}).get("input")
            props = {"callID": part.get("id"), "tool": part.get("tool"), "input": saisie}
        call_id = str(props.get("callID") or props.get("id") or evt.get("id") or "outil")
        outil = str(props.get("tool") or "outil")
        info = _outil_base(call_id, outil, saisie, racine)
        outils[call_id] = info
        return [("activite", info.copy())]
    if genre in {"session.next.tool.progress", "session.next.tool.success", "session.next.tool.failed"}:
        call_id = str(props.get("callID") or evt.get("id") or "outil")
        info = dict(outils.get(call_id, {"id": call_id, "type": "tool", "title": "Outil"}))
        if genre.endswith("progress"):
            info["status"] = "running"
        elif genre.endswith("success"):
            info["status"] = "success"
        else:
            info["status"] = "error"
            info["error"] = _sortie(props.get("error"))
        contenu = _sortie(props.get("content") or props.get("result") or props.get("structured"))
        if contenu:
            info["output"] = contenu[:OUTIL_RESUME_MAX]
        if props.get("outputPaths"):
            info["fichiers"] = [_chemin_relatif(str(p), racine) or str(p) for p in props["outputPaths"]]
        outils[call_id] = info
        return [("activite", info)]

    if genre in {"permission.asked", "permission.v2.asked"}:
        request_id = str(props.get("id") or props.get("requestID") or evt.get("id"))
        action = props.get("permission") or props.get("action") or "outil"
        resources = props.get("patterns") or props.get("resources") or []
        return [("permission", {
            "id": request_id, "status": "waiting_for_permission",
            "action": str(action), "resources": resources, "session": sid,
        })]
    if genre in {"permission.replied", "permission.v2.replied"}:
        request_id = str(props.get("requestID") or props.get("id") or evt.get("id"))
        reply = str(props.get("reply") or "")
        return [("permission", {
            "id": request_id,
            "status": "success" if reply != "reject" else "rejected",
            "reply": reply,
        })]
    if genre == "file.edited":
        fichier = props.get("file")
        chemin = _chemin_relatif(fichier, racine)
        return [("activite", {
            "id": str(evt.get("id") or f"file:{chemin}"),
            "type": "file_write",
            "status": "success",
            "title": "Fichier modifié",
            "fichier": chemin,
        })] if chemin else sorties
    if genre == "session.diff":
        diff = props.get("diff")
        return [("activite", {
            "id": str(evt.get("id") or "diff"),
            "type": "diff",
            "status": "success",
            "title": "Diff réel",
            "diff": diff,
        })] if diff else sorties
    if genre == "session.error":
        return [("erreur", {
            "code": "opencode",
            "message": _sortie(props.get("error")) or "Erreur OpenCode",
        })]
    if genre == "session.idle":
        return [("activite", {
            "id": f"session:{sid}:status",
            "type": "thinking",
            "status": "success",
            "title": "Analyse terminée",
        }), ("fin", {"session": sid})]
    if genre == "session.status":
        statut = props.get("status")
        statut = statut.get("type") if isinstance(statut, dict) else statut
        statut = str(statut or "unknown")
        # L'activité « Analyse OpenCode » porte un id CONSTANT, pour que la
        # suivante vienne la remplacer sur place plutôt que d'empiler une ligne.
        # Il faut donc la REFERMER quand le statut quitte « busy » : sans cet
        # événement de clôture, elle restait affichée « ● En cours… » pour toute
        # la session, l'interface n'ayant aucun moyen de savoir qu'elle avait
        # fini. Le frontend remplace un item dès qu'un événement réutilise son id.
        if statut == "busy":
            return [("etat", {"status": statut}), ("activite", {
                "id": f"session:{sid}:status",
                "type": "thinking",
                "status": "running",
                "title": "Analyse OpenCode",
                "description": "OpenCode prépare sa prochaine action…",
            })]
        return [("etat", {"status": statut}), ("activite", {
            "id": f"session:{sid}:status",
            "type": "thinking",
            "status": "success",
            "title": "Analyse OpenCode",
            "description": "OpenCode a terminé cette étape.",
        })]
    if genre == "command.executed":
        commande = props.get("name") or props.get("arguments")
        return [("activite", {
            "id": str(evt.get("id") or "command"),
            "type": "terminal",
            "status": "success",
            "title": "Commande terminée",
            "commande": _sortie(commande),
        })]
    return sorties


async def executer_tache_serveur(
    racine: Path, message: str, sid_opencode: str | None = None,
    timeout: float = TACHE_TIMEOUT,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    from . import opencode

    try:
        await asyncio.to_thread(opencode.assurer_serveur_agent, racine)
        titre = " ".join(message.split())[:80] or "Tâche OpenCode"
        # `agent=True` : la session doit vivre sur le serveur DÉDIÉ (port 4097),
        # celui qui porte `AISTUDIO_AGENT`. Sur le serveur de discussion elle
        # serait injoignable pour /permission/{id}/reply.
        sid = sid_opencode or await asyncio.to_thread(
            opencode.creer_session, racine, titre, True
        )
        yield ("debut", {"session": sid, "moteur": "opencode", "mode": "server"})
        outils: dict[str, Any] = {}
        async for evt in opencode.flux_tache(racine, sid, message, True, timeout):
            for nom, data in mapper_evenement_serveur(evt, racine, outils):
                yield (nom, data)
                if nom == "fin":
                    return
        yield ("fin", {"session": sid})
    except opencode.ErreurOpenCode as exc:
        raise ErreurTache(exc.code, exc.message) from exc


def _terminer_arbre(processus: Any) -> None:
    """Tue le processus ET toute sa descendance.

    `Popen.kill()` ne vise que le processus lancé — ici `opencode.exe`. Or la
    commande shell de l'agent (`npm run dev`, un serveur, un watcher) est un
    petit arbre : `opencode` → `cmd` → `npm` → `node`. Tuer le seul parent
    laissait les enfants vivants, orphelins, toujours attachés au pipe de
    sortie. Constaté : 4 arbres `npm run dev` survivant à l'annulation, et la
    tâche qui restait « active » indéfiniment. `taskkill /T` coupe l'arbre.
    """
    if processus is None:
        return
    pid = getattr(processus, "pid", None)
    if sys.platform.startswith("win") and pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:  # noqa: BLE001 — on retente avec le kill simple
            pass
    try:
        processus.kill()
    except Exception:  # noqa: BLE001 — processus déjà mort
        pass


# Processus `opencode` en cours, par projet. L'API s'en sert pour annuler une
# tâche qui pend : un simple abort HTTP ne suffit pas en mode CLI.
_PROCESSUS: dict[str, Any] = {}


def interrompre_tache(racine: str | Path) -> bool:
    """Annule la tâche en cours sur ce projet. Vrai si un processus a été tué."""
    processus = _PROCESSUS.get(str(racine))
    if processus is None:
        return False
    _terminer_arbre(processus)
    return True


async def _ligne(processus: Any, budget: float) -> str | None:
    """Lit une ligne du flux de l'agent, ou None si le budget de temps est épuisé.

    DEUX pièges corrigés ici, tous deux reproduits sur un vrai projet :

    1. `await asyncio.to_thread(readline)` ne rend jamais la main si l'agent est
       muet. Le délai de fin de tâche n'étant revérifié qu'AVANT l'appel, une
       commande sans fin (`npm run dev`) neutralisait le timeout : la tâche
       pendait indéfiniment.

    2. `asyncio.wait_for(to_thread(...))` ne corrige PAS le premier point :
       annuler l'attente n'arrête pas le thread, et `wait_for` reste bloqué
       jusqu'à ce que le thread se termine — mesuré : 30 s pour un budget de
       0,6 s, soit exactement la durée du faux `readline`.

    D'où le montage explicite : un thread deamon lit et dépose dans une file,
    et la boucle async la sonde en respectant le budget. Le thread peut rester
    bloqué, il est deamon : il se libère seul quand l'arbre est tué et que le
    pipe se ferme.
    """
    resultat: queue.Queue = queue.Queue(maxsize=1)

    def _lire() -> None:
        try:
            resultat.put(processus.stdout.readline())
        except Exception as exc:  # noqa: BLE001 — remontée à la boucle
            resultat.put(exc)

    threading.Thread(target=_lire, daemon=True).start()
    fin = time.monotonic() + max(budget, 0.0)
    while True:
        try:
            valeur = resultat.get_nowait()
        except queue.Empty:
            if time.monotonic() >= fin:
                return None
            await asyncio.sleep(0.05)
            continue
        if isinstance(valeur, Exception):
            raise valeur
        return valeur


async def executer_tache(
    racine: Path,
    message: str,
    sid_opencode: str | None = None,
    binaire: str | None = None,
    processus: Any = None,
    timeout: float = TACHE_TIMEOUT,
    moteur: str = "serveur",
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Générateur d'événements du mode autonome (consommé par la route SSE).

    *processus* permet l'injection d'un faux processus en test ; s'il est None,
    le processus réel est démarré. Les événements sortent sous forme de tuples
    (nom, data) — la couche API les transforme en événements SSE.

    `moteur="serveur"` (défaut) passe par le serveur OpenCode DÉDIÉ : c'est le
    seul chemin où une demande de permission peut être présentée à l'utilisateur,
    `opencode run --auto` approuvant tout silencieusement. `moteur="cli"` garde
    l'ancien comportement et reste utilisé par les tests.

    Un processus injecté force toujours le mode CLI : les tests de l'ancien
    chemin ne doivent pas se retrouver à parler à un vrai serveur.
    """
    if not message.strip():
        raise ErreurTache("schema_invalide", "Consigne vide.")

    if moteur == "serveur" and processus is None:
        async for item in executer_tache_serveur(racine, message, sid_opencode, timeout):
            yield item
        return

    if processus is None:
        try:
            bin_ = binaire or resoudre_binaire()
        except ErreurTache:
            raise
        try:
            processus = _demarrer(bin_, racine, message, sid_opencode)
        except OSError as exc:
            raise ErreurTache("moteur_indisponible", f"Impossible de lancer l'agent : {exc}") from exc

    textes: dict[str, str] = {}
    activites: dict[str, Any] = {}
    diagnostic: list[str] = []
    sid_courant: str | None = sid_opencode
    debut_emis = False
    fin_deadline = time.monotonic() + timeout
    evenements_emois = 0
    # Enregistré pour que l'API puisse tuer l'arbre en cas d'annulation : en
    # mode CLI, l'appel HTTP d'avortement ne rejoint aucun serveur.
    _PROCESSUS[str(racine)] = processus

    try:
        while True:
            restant = fin_deadline - time.monotonic()
            if restant <= 0:
                _terminer_arbre(processus)
                yield ("erreur", {
                    "code": "timeout",
                    "message": f"Tâche annulée au-delà de {int(timeout)} s.",
                })
                return
            try:
                ligne = await _ligne(processus, restant)
            except Exception as exc:  # noqa: BLE001 — lecture du flux
                yield ("erreur", {
                    "code": "interne",
                    "message": f"Flux de sortie de l'agent interrompu : {exc}",
                })
                return
            if ligne is None:
                # Délai dépassé pendant une lecture : l'agent est muet, très
                # probablement parce qu'il a lancé un `dev` qui ne se termine
                # jamais. On coupe l'arbre, sinon ses enfants survivent.
                _terminer_arbre(processus)
                yield ("erreur", {
                    "code": "timeout",
                    "message": (
                        f"Tâche annulée au-delà de {int(timeout)} s. "
                        "Une commande lancée par l'agent ne se termine pas "
                        "(serveur de développement ?) : faites-la tourner en "
                        "arrière-plan."
                    ),
                })
                return
            if ligne == "":
                break

            try:
                evt_brut = json.loads(ligne.strip())
            except json.JSONDecodeError:
                evt_brut = None
            if evt_brut is None:
                # stderr est fusionné dans stdout : sans cela, la vraie cause
                # d'un arrêt (session inconnue, quota, réseau) disparaît et
                # l'utilisateur ne voit que « code 1 ».
                bruit = ligne.strip()
                if bruit:
                    diagnostic.append(bruit)
            if isinstance(evt_brut, dict):
                sid_lu = _sid_ligne(evt_brut)
                if sid_lu:
                    sid_courant = sid_lu

            if not debut_emis and sid_courant:
                debut_emis = True
                yield ("debut", {
                    "session": sid_courant, "moteur": "opencode", "mode": "auto",
                })

            echec_fournisseur = False
            for nom, data in mapper_ligne(
                ligne, textes, racine=racine, activites=activites
            ):
                evenements_emois += 1
                yield (nom, data)
                if nom == "erreur":
                    # Le fournisseur a tranché (quota, clé refusée, modèle
                    # retiré) : continuer à lire ne rapporterait qu'un « code 1 »
                    # trente secondes plus tard, sans cause.
                    echec_fournisseur = True
            if echec_fournisseur:
                return

        if not debut_emis:
            debut_emis = True
            # `session` vaut None tant qu'aucun identifiant OpenCode n'a été lu.
            # Un faux identifiant ici serait mémorisé par l'API comme s'il était
            # réel, puis renvoyé à la requête suivante via `-s` : OpenCode
            # répond « Session not found » et le tour suivant échoue à son tour,
            # définitivement. Un échec transitoire casserait la session à vie.
            yield ("debut", {
                "session": sid_courant, "moteur": "opencode", "mode": "auto",
            })
        code = getattr(processus, "returncode", None)
        if code is None:
            code = processus.wait(timeout=10)
        if code != 0 and evenements_emois == 0:
            # Les lignes non-JSON sont du diagnostic OpenCode : elles expliquent
            # l'échec. On les montre, en ignorant les codes couleur ANSI.
            cause = " ".join(diagnostic)
            cause = _SANS_ANSI.sub("", cause).strip()
            yield ("erreur", {
                "code": "interne",
                "message": (
                    f"L'agent s'est arrêté avec le code {code}."
                    + (f" {cause[:300]}" if cause else "")
                ),
            })
            return
        yield ("fin", {"session": sid_courant})
    finally:
        _PROCESSUS.pop(str(racine), None)
        if processus and getattr(processus, "poll", None) is not None:
            _terminer_arbre(processus)
