"""Smart File Organizer — configuration (port de l'ancienne app Streamlit).

Module métier isolé (stdlib uniquement) : scan d'un dossier, catégorisation par
extension, déplacement dans des dossiers de catégories, historique SQLite.
"""

from __future__ import annotations

from pathlib import Path

from ...config import DATA_DIR, PROJETS_DIR

# Dossier (frère du dossier source) où sont créées les catégories.
BASE_OUTPUT_DIR = "organized_files"

# Mapping des extensions vers les catégories.
EXTENSION_MAP = {
    # Documents
    ".pdf": "Documents",
    ".doc": "Documents",
    ".docx": "Documents",
    ".txt": "Documents",
    ".odt": "Documents",
    ".rtf": "Documents",
    ".md": "Documents",
    # Images
    ".jpg": "Images",
    ".jpeg": "Images",
    ".png": "Images",
    ".gif": "Images",
    ".bmp": "Images",
    ".svg": "Images",
    ".webp": "Images",
    # Vidéos
    ".mp4": "Vidéos",
    ".avi": "Vidéos",
    ".mov": "Vidéos",
    ".mkv": "Vidéos",
    ".webm": "Vidéos",
    # Musiques
    ".mp3": "Musique",
    ".wav": "Musique",
    ".flac": "Musique",
    ".ogg": "Musique",
    # Archives
    ".zip": "Archives",
    ".rar": "Archives",
    ".7z": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    ".tar.gz": "Archives",
    # Programmes
    ".exe": "Programmes",
    ".msi": "Programmes",
    ".apk": "Programmes",
    ".py": "Programmes",
    ".js": "Programmes",
}

# Emplacement de la base d'historique (remplaçable par env ORGANIZER_DB).
_DB_ENV = __import__("os").getenv("ORGANIZER_DB", "")
DB_PATH = Path(_DB_ENV) if _DB_ENV else DATA_DIR / "organizer.db"

# Dossiers qu'on ne doit jamais réorganiser (espace projets de AI Studio).
REPERTOIRES_INTERDITS = [PROJETS_DIR]