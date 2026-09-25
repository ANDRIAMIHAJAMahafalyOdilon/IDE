"""Extraction du texte brut des documents de cours (PDF, TXT, MD).

Portage de `etape2_extraction_pdf.py` (projet « AI ») dans l'architecture
AI Studio : en plus des PDF via pypdf, on accepte désormais les fichiers
texte (classes, notes) pour alimenter l'indexation.
"""

from __future__ import annotations

from pathlib import Path


def _lire_pdf(chemin: Path) -> list[dict]:
    """Une entrée par page du PDF : {page, texte} (pypdf)."""
    from pypdf import PdfReader

    pages = []
    for numero, page in enumerate(PdfReader(chemin).pages, start=1):
        texte = page.extract_text() or ""
        pages.append({"page": numero, "texte": texte.strip()})
    return pages


def _lire_texte(chemin: Path) -> list[dict]:
    """Un fichier texte = une seule « page » (page 1)."""
    texte = chemin.read_text(encoding="utf-8", errors="replace").strip()
    if not texte:
        return []
    return [{"page": 1, "texte": texte}]


def extraire_texte(chemin: str | Path) -> list[dict]:
    """Extrait le texte d'un document. Retourne [{page, texte}, ...].

    Formats supportés : .pdf (pypdf), .txt, .md. Lève ValueError pour un
    format inconnu et FileNotFoundError si le fichier n'existe pas.
    """
    chemin = Path(chemin)
    if not chemin.is_file():
        raise FileNotFoundError(f"Fichier introuvable : {chemin}")

    extension = chemin.suffix.lower()
    if extension == ".pdf":
        return _lire_pdf(chemin)
    if extension in (".txt", ".md"):
        return _lire_texte(chemin)
    raise ValueError(f"Format non supporté : {extension} (pdf, txt ou md)")