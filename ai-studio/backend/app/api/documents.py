"""API des documents de cours (upload, liste, indexation RAG, suppression).

Regroupe les fonctionnalités du projet « AI » (Streamlit) désormais gérées
par le backend AI Studio : les documents vivent dans `DOCUMENTS_DIR`
(ai-studio/data/documents) et l'index FAISS — construit par reindexation —
dans ai-studio/data/index.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..config import DOCUMENTS_DIR
from ..services.rag import embeddings as rag_emb

router = APIRouter(prefix="/api/documents", tags=["documents"])

EXTENSIONS = {".pdf", ".txt", ".md"}


def _nom_propre(nom: str) -> str:
    """Assainit un nom de fichier : jamais de chemin sur disque."""
    propre = Path(nom or "").name.strip()
    if not propre or propre in (".", ".."):
        raise ValueError("nom de fichier invalide")
    return propre


def _ajouter_document(chemin: Path) -> dict:
    return {
        "nom": chemin.name,
        "octets": chemin.stat().st_size,
        "extension": chemin.suffix.lower(),
    }


@router.get("")
def lister_documents() -> dict:
    """Documents présents + état de l'index RAG (existe, nb_chunks, sources)."""
    documents = [
        _ajouter_document(chemin)
        for chemin in sorted(DOCUMENTS_DIR.glob("*"))
        if chemin.is_file() and not chemin.name.startswith(".")
    ]
    return {"documents": documents, "index": rag_emb.etat_index()}


@router.post("/upload")
async def uploader_document(fichier: UploadFile = File(...)) -> dict:
    """Enregistre un document (.pdf, .txt, .md) pour l'indexation."""
    nom = _nom_propre(fichier.filename)
    extension = Path(nom).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"format non supporté : {extension} (pdf, txt ou md)",
        )
    contenu = await fichier.read()
    if not contenu:
        raise HTTPException(status_code=400, detail="fichier vide")
    cible = DOCUMENTS_DIR / nom
    cible.write_bytes(contenu)
    return {"nom": nom, "octets": len(contenu)}


@router.post("/reindex")
def reindexer_index() -> dict:
    """Reconstruit l'index FAISS sur l'ensemble des documents présents.

    Appel un peu lourd (embeddings Gemini) : exécuté hors de la boucle
    d'événements (endpoint synchrone). Retourne {nb_documents, nb_chunks, ...}.
    """
    try:
        resume = rag_emb.construire_index_documents()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — erreur d'embedding réseau
        raise HTTPException(status_code=500, detail=f"indexation impossible : {exc}") from exc
    return resume


@router.delete("/{nom}")
def supprimer_document(nom: str) -> dict:
    """Supprime un document du dossier (ne touche pas à l'index)."""
    nom = _nom_propre(nom)
    cible = DOCUMENTS_DIR / nom
    if not cible.is_file():
        raise HTTPException(status_code=404, detail=f"document introuvable : {nom}")
    cible.unlink()
    return {"supprime": nom}