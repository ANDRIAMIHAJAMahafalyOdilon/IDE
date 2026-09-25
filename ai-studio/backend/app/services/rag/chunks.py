"""Découpage du texte extrait en chunks de ~200 mots.

Portage de `etape3_chunking_embeddings.py` (projet « AI »). Chaque chunk
reçoit une clé `source` (nom du document) pour que la recherche retourne
des passages traçables (fichier + page).
"""

from __future__ import annotations

from ...config import RAG_CHEVAUCHEMENT, RAG_TAILLE_CHUNK


def decouper_en_chunks(pages_texte: list[dict], source: str = "document") -> list[dict]:
    """Découpe chaque page en chunks de ~RAG_TAILLE_CHUNK mots.

    Un léger chevauchement (RAG_CHEVAUCHEMENT mots) évite de couper une idée
    en deux à la frontière de deux chunks. Retourne
    [{source, page, texte}, ...].
    """
    chunks: list[dict] = []
    for page in pages_texte:
        if not page.get("texte"):
            continue
        mots = page["texte"].split()
        debut = 0
        while debut < len(mots):
            fin = debut + RAG_TAILLE_CHUNK
            morceau = " ".join(mots[debut:fin])
            chunks.append({
                "source": source,
                "page": page.get("page", 1),
                "texte": morceau,
            })
            debut += RAG_TAILLE_CHUNK - RAG_CHEVAUCHEMENT
    return chunks