"""AI Studio — configuration centrale.

Lit le fichier .env (racine du dépôt ai-studio/), définit les chemins absolus
et les variables d'environnement nécessaires aux services.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

# --- Deux racines bien distinctes, et la confusion entre les deux casse tout ---
#
# SOURCE : racine du dépôt ai-studio/ (équivaut à parents[2] de app/config.py).
# BUNDLE : ce que l'exécutable embarque. En onefile, PyInstaller extrait dans un
#          dossier temporaire qui est SUPPRIMÉ à la fermeture : tout ce qu'on y
#          écrit est perdu, et `__file__` pointe dedans, pas dans le dépôt.
# DONNÉES : doit survivre aux lancements, donc jamais dans le bundle.
#
# Sans cette distinction, un .exe repartait de zéro à chaque démarrage : projets,
# sessions et index partaient dans le dossier temporaire.
GEL = bool(getattr(sys, "frozen", False))  # noqa: GEL = gelée en packaging
if GEL:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    _donnees_defaut = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AIStudio" / "data"
    _config_defaut = Path(os.environ.get("APPDATA") or Path.home()) / "AIStudio"
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    BUNDLE_DIR = PROJECT_ROOT
    _donnees_defaut = PROJECT_ROOT / "data"
    _config_defaut = PROJECT_ROOT

if GEL:
    # Le .env de l'utilisateur vit hors du bundle : un secret embarqué dans
    # l'exécutable serait extractible par n'importe qui, et illisible en écriture.
    load_dotenv(_config_defaut / ".env")
else:
    # .env au niveau racine du dépôt, puis fallback backend/.env
    load_dotenv(BUNDLE_DIR / ".env")
    load_dotenv(BUNDLE_DIR / "backend" / ".env", override=False)


_AGENTS_REQUIS = ("discussion", "proposition")


def _config_a_les_agents(chemin: Path, agents: tuple[str, ...]) -> bool:
    """Vrai si `chemin` déclare chacun des `agents` demandés.

    Détection par recherche du nom quoted plutôt que par parsing JSON : le
    fichier est du JSONC (commentaires autorisés), et OpenCode n'expose pas de
    parseur côté Python ici. Les noms cherchés sont des identifiants que nous
    écrivons nous-mêmes, donc la recherche de `"nom"` est suffisante — et une
    config illisible vaut « pas à jour », ce qui déclenche le rafraîchissement.
    """
    try:
        texte = chemin.read_text(encoding="utf-8")
    except OSError:
        return False
    return all(f'"{nom}"' in texte for nom in agents)


def _preparer_config_opencode() -> Path | None:
    """Chemin de la config OpenCode À UTILISER, ou None si introuvable.

    En exécutable, la config embarquée est une COPIE figée au moment du build :
    la modifier dans le dépôt ne changeait rien au .exe, et l'agent continuait de
    tourner avec l'ancienne valeur. Erreur constatée après avoir passé `bash` de
    "deny" à "allow" — le bundle contenait toujours "deny", et l'agent répétait
    « no shell/bash tool available » malgré la correction.

    On dépose donc une copie MODIFIABLE dans %APPDATA%\\AIStudio, créée depuis le
    bundle au premier lancement. Corriger la config devient une édition de
    fichier, sans repackaging. Le bundle ne sert plus que de graine.
    """
    if not GEL:
        return BUNDLE_DIR / "opencode.jsonc"
    bundle = BUNDLE_DIR / "opencode.jsonc"
    utilisateur = _config_defaut / "opencode.jsonc"
    if bundle.is_file():
        try:
            if not utilisateur.is_file():
                utilisateur.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle, utilisateur)
            elif not _config_a_les_agents(utilisateur, _AGENTS_REQUIS):
                # Mise à jour : la config semée lors d'un lancement précédent
                # n'a pas les agents ajoutés depuis. Sans cette rafraîchissement,
                # l'application démarrerait avec une config périmée et
                # l'Edit retombait en silence sur l'agent par défaut — qui, lui,
                # a le droit d'écrire et d'exécuter des commandes. Le serveur
                # OpenCode lisant sa config au DÉMARRAGE, il faut aussi le
                # redémarrer, ce que fait `opencode.assurer_serveur`.
                # L'ancien fichier est conservé : la config reste éditable à la
                # main, on ne veut pas perdre une correction de l'utilisateur.
                shutil.copy2(utilisateur, utilisateur.with_suffix(".jsonc.bak"))
                shutil.copy2(bundle, utilisateur)
        except OSError:
            if utilisateur.is_file():
                return utilisateur
            return bundle
    if utilisateur.is_file():
        return utilisateur
    return bundle if bundle.is_file() else None


_CONFIG_OPENCODE = _preparer_config_opencode()

# Dossier qui contient les projets importés (arborescences téléchargées / créées).
# Surchargeable (DATA_DIR) pour pointer vers un disque persistant ou un dossier
# éphémère d'un déploiement cloud ; tous les chemins de données en découlent.
DATA_DIR = Path(os.getenv("DATA_DIR", str(_donnees_defaut)))

# Build du frontend Vite. En développement il n'existe pas (Vite sert la SPA sur
# 5173 et proxifie /api) ; en production le backend sert directement dist/,
# ce qui évite un second domaine et tout config CORS.
FRONTEND_DIST = Path(
    os.getenv("FRONTEND_DIST", str(BUNDLE_DIR / "frontend" / "dist"))
)
PROJETS_DIR = Path(os.getenv("PROJETS_DIR", str(DATA_DIR / "projets")))
# Mémoire durable des fils Chat/Edit. Elle ne dépend plus de la durée de vie
# du processus backend (redémarrage du serveur ou rechargement en développement).
MEMOIRE_DIR = Path(os.getenv("MEMOIRE_DIR", str(DATA_DIR / "sessions")))

# Serveur OpenCode local (voir opencode_client.py).
OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/")

# Serveur DEDIE au mode autonome (agent). Port distinct de celui du discussion :
# c'est la garantie STRUCTURELLE que la session interactive de l'utilisateur ne
# peut pas hériter de `AISTUDIO_AGENT=1`, qui active la réécriture des commandes
# longues en arrière-plan. Un port différent rend le partage impossible par
# construction — pas par une règle vérifiée à l'exécution, qui peut être contournée.
OPENCODE_AGENT_BASE_URL = os.getenv(
    "OPENCODE_AGENT_BASE_URL", "http://127.0.0.1:4097"
).rstrip("/")
OPENCODE_TIMEOUT = float(os.getenv("OPENCODE_TIMEOUT", "90"))

# Binaire CLI OpenCode pour le MODE AUTONOME (agent qui exécute réellement) :
# vide = résolution automatique (npm globals), sinon chemin explicite.
OPENCODE_BIN = os.getenv("OPENCODE_BIN", "")
# Modèle utilisé par le mode autonome. Il est passé explicitement à la CLI :
# les projets ouverts sous data/projets/ ou en mode DIRECT ne possèdent pas
# forcément leur propre opencode.jsonc.
OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "opencode/big-pickle")
# Dossier neutre du serveur OpenCode pour le mode Chat. Le Chat ne travaille pas
# dans un projet : son contexte est déjà assemblé dans le prompt (arborescence,
# mémoire, fichiers), donc aucun outil ne lui est utile. Ce dossier ne sert qu'à
# satisfaire l'exigence de `directory` du serveur. Il ne doit PAS être le projet
# de l'utilisateur : un dossier neutre rend impossible toute lecture ou écriture
# fortuite dans ses fichiers.
OPENCODE_CHAT_DIR = Path(os.getenv("OPENCODE_CHAT_DIR", str(DATA_DIR / "chat")))
# Config OpenCode de l'application (opencode.jsonc à la racine de ai-studio/).
# Elle porte les instructions de conduite de l'agent et ses permissions.
# Elle DOIT être passée au serveur via la variable OPENCODE_CONFIG : le serveur
# est lancé avec `cwd` sur le projet de l'utilisateur, or OpenCode y découvre
# la config de CE projet et ignore donc la nôtre. Sans ce env var, le serveur
# tourne avec les valeurs par défaut — tout autorisé, aucune consigne de
# conduite — et l'agent explore le projet au lieu d'agir.
OPENCODE_CONFIG = Path(
    os.getenv("OPENCODE_CONFIG", str(_CONFIG_OPENCODE or (BUNDLE_DIR / "opencode.jsonc")))
)

# Garde-fou : `is_file()` et pas `exists()`. Un `datas` mal écrit produit un
# DOSSIER nommé opencode.jsonc (le fichier est copié À L'INTÉRIEUR), et l'agent
# échoue alors sur « BadResource: FileSystem.readFile » au premier message.
if not OPENCODE_CONFIG.is_file():
    raise FileNotFoundError(
        f"Config OpenCode introuvable ou illisible : {OPENCODE_CONFIG}. "
        "En exécutable, le .spec doit embarquer opencode.jsonc à la RACINE du "
        'bundle (datas = [(source, ".")], pas (source, "opencode.jsonc")).'
    )
# Durée maximale d'une tâche agent autonome (secondes).
TACHE_TIMEOUT = float(os.getenv("TACHE_TIMEOUT", "1200"))
# Longueur maximale d'un résumé de sortie d'outil (events `outil`).
OUTIL_RESUME_MAX = int(os.getenv("OUTIL_RESUME_MAX", "600"))
# Longueur maximale de l'aperçu de modification affiché dans la timeline.
OUTIL_APERCU_MAX = int(os.getenv("OUTIL_APERCU_MAX", "4000"))

# Clés API (le cas échéant).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Délais et modèles des moteurs LLM (fallback Gemini/Groq).
AI_TIMEOUT_MS = int(os.getenv("AI_TIMEOUT_MS", "20000"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Budgets du contexte envoyé à l'agent (anti-explosion de tokens).
# Calibrés pour rester SOUS la limite la plus basse des moteurs de repli
# (Groq `openai/gpt-oss-120b` = 8 000 TPM, ≈3,7 car./token) :
# ~14 000 car. ≈ 3 800 tokens → un prompt + sa réponse tiennent dans la minute.
ARBRE_CONTEXTE_MAX = int(os.getenv("ARBRE_CONTEXTE_MAX", "40"))
ARBORESCENCE_CACHE_SECONDES = float(os.getenv("ARBORESCENCE_CACHE_SECONDES", "5"))
# Par fichier de contexte. 2 500 car. ne laissaient que ~10 % d'un fichier de
# 26 ko : l'agent ne pouvait ni voir la région à modifier, ni la restituer. Avec
# le patch par numéros de ligne, il suffit de voir la bonne fenêtre, mais cette
# fenêtre doit exister — d'où 9 000 car., soit ~2 400 lignes.
FICHIER_CONTEXTE_MAX_CAR = int(os.getenv("FICHIER_CONTEXTE_MAX_CAR", "9000"))
# Le préfixe «   12 | » coûte 8 caractères par ligne : sans plafond de lignes,
# un fichier uniquement fait de retours à la ligne transformerait 9 000 car. en
# ~80 000. 1 200 lignes suffisent largement à situer une modification.
FICHIER_CONTEXTE_MAX_LIGNES = int(os.getenv("FICHIER_CONTEXTE_MAX_LIGNES", "1200"))
FICHIERS_CONTEXTE_MAX = int(os.getenv("FICHIERS_CONTEXTE_MAX", "3"))
CONTEXTE_MAX_CAR = int(os.getenv("CONTEXTE_MAX_CAR", "14000"))

# Budget du mode EDIT, volontairement plus large que celui du Chat.
# Le patch se fait par numéros de ligne : si la ligne à modifier n'est pas dans
# le contexte, l'agent ne peut pas la situer et répond `[]` — c'est exactement
# ce qui se passait sur un fichier de 26 ko, dont la ligne 247 tombait au-delà
# du plafond. 40 000 car. couvre la quasi-totalité des fichiers source usuels en
# un seul envoi (donc sans second aller-retour, donc sans latence ajoutée).
# Le Chat garde CONTEXTE_MAX_CAR : il n'a pas besoin du fichier entier et reste
# sous le quota de 8 000 TPM de Groq.
CONTEXTE_EDIT_MAX_CAR = int(os.getenv("CONTEXTE_EDIT_MAX_CAR", "40000"))
FICHIER_EDIT_MAX_CAR = int(os.getenv("FICHIER_EDIT_MAX_CAR", "40000"))
FICHIER_EDIT_MAX_LIGNES = int(os.getenv("FICHIER_EDIT_MAX_LIGNES", "3000"))
MEMOIRE_ECHANGES_MAX = int(os.getenv("MEMOIRE_ECHANGES_MAX", "12"))

# RAG — documents de cours (PDF/TXT/MD) et index FAISS.
DOCUMENTS_DIR = Path(os.getenv("DOCUMENTS_DIR", str(DATA_DIR / "documents")))
INDEX_DIR = Path(os.getenv("INDEX_DIR", str(DATA_DIR / "index")))
MODELE_EMBEDDING = os.getenv("MODELE_EMBEDDING", "gemini-embedding-001")
RAG_TAILLE_CHUNK = int(os.getenv("RAG_TAILLE_CHUNK", "200"))
RAG_CHEVAUCHEMENT = int(os.getenv("RAG_CHEVAUCHEMENT", "30"))
RAG_K = int(os.getenv("RAG_K", "3"))

# CORS : en dev, autorise le serveur Vite.
# inutile en exécutable : la SPA est servie par le backend, donc même origine.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]

# Journaux. Un exécutable compilé `--noconsole` n'affiche rien : sans ce fichier,
# un plantage au démarrage serait totalement muet.
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(DATA_DIR / "logs")))


def assurer_repertoires() -> None:
    """Crée les dossiers de données au démarrage."""
    PROJETS_DIR.mkdir(parents=True, exist_ok=True)
    MEMOIRE_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    OPENCODE_CHAT_DIR.mkdir(parents=True, exist_ok=True)
