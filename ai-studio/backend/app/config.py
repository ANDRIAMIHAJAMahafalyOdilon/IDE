"""AI Studio — configuration centrale.

Lit le fichier .env (racine du dépôt ai-studio/), définit les chemins absolus
et les variables d'environnement nécessaires aux services.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Racine du dépôt ai-studio/ (équivaut à parents[2] de app/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# .env au niveau racine du dépôt, puis fallback backend/.env
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / "backend" / ".env", override=False)

# Dossier qui contient les projets importés (arborescences téléchargées / créées).
DATA_DIR = PROJECT_ROOT / "data"
PROJETS_DIR = Path(os.getenv("PROJETS_DIR", str(DATA_DIR / "projets")))

# Serveur OpenCode local (voir opencode_client.py).
OPENCODE_BASE_URL = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/")
OPENCODE_TIMEOUT = float(os.getenv("OPENCODE_TIMEOUT", "90"))

# Binaire CLI OpenCode pour le MODE AUTONOME (agent qui exécute réellement) :
# vide = résolution automatique (npm globals), sinon chemin explicite.
OPENCODE_BIN = os.getenv("OPENCODE_BIN", "")
# Modèle utilisé par le mode autonome. Il est passé explicitement à la CLI :
# les projets ouverts sous data/projets/ ou en mode DIRECT ne possèdent pas
# forcément leur propre opencode.jsonc.
OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "opencode/big-pickle")
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
FICHIER_CONTEXTE_MAX_CAR = int(os.getenv("FICHIER_CONTEXTE_MAX_CAR", "2500"))
FICHIERS_CONTEXTE_MAX = int(os.getenv("FICHIERS_CONTEXTE_MAX", "3"))
CONTEXTE_MAX_CAR = int(os.getenv("CONTEXTE_MAX_CAR", "14000"))
MEMOIRE_ECHANGES_MAX = int(os.getenv("MEMOIRE_ECHANGES_MAX", "4"))

# RAG — documents de cours (PDF/TXT/MD) et index FAISS.
DOCUMENTS_DIR = Path(os.getenv("DOCUMENTS_DIR", str(DATA_DIR / "documents")))
INDEX_DIR = Path(os.getenv("INDEX_DIR", str(DATA_DIR / "index")))
MODELE_EMBEDDING = os.getenv("MODELE_EMBEDDING", "gemini-embedding-001")
RAG_TAILLE_CHUNK = int(os.getenv("RAG_TAILLE_CHUNK", "200"))
RAG_CHEVAUCHEMENT = int(os.getenv("RAG_CHEVAUCHEMENT", "30"))
RAG_K = int(os.getenv("RAG_K", "3"))

# CORS : en dev, autorise le serveur Vite.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]


def assurer_repertoires() -> None:
    """Crée les dossiers de données au démarrage."""
    PROJETS_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
