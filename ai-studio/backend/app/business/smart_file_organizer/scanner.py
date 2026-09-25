"""Fonctions de scan et de catégorisation (port).

Lit EXTENSION_MAP via le module config (patchable par les tests).
"""

from __future__ import annotations

from pathlib import Path

from . import config


def _ignore(nom: str) -> bool:
    """Rejette la destination organisée elle-même, les dépôts git et Caches."""
    return nom == config.BASE_OUTPUT_DIR or nom in {".git", "__pycache__"}


def list_files(root: Path) -> list[Path]:
    """Fichiers (récursif) sous *root*, hors dossiers parasites."""
    return [
        p for p in root.rglob("*")
        if p.is_file() and not any(_ignore(part) for part in p.relative_to(root).parts)
    ]


def categorize_file(file_path: Path) -> tuple[str, str]:
    """(catégorie, extension). Extension inconnue -> « Divers »."""
    ext = file_path.suffix.lower()
    if ext == ".gz" and file_path.name.lower().endswith(".tar.gz"):
        ext = ".tar.gz"
    category = config.EXTENSION_MAP.get(ext, "Divers")
    return category, ext