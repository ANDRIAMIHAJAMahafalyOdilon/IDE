"""Embeddings Gemini + index FAISS pour la recherche sur les documents de cours.

Portage de `etape3_chunking_embeddings.py` (projet « AI ») :
- `construire_index_documents()` : ré-indexe TOUS les documents du dossier
  `DOCUMENTS_DIR` (extraction -> chunks -> embeddings -> FAISS -> disque).
- `rechercher(question)` : charge l'index persistant et retourne les K chunks
  les plus proches sémantiquement, avec leur source et leur page.

Le client Gemini et FAISS sont importés paresseusement dans les fonctions :
le backend démarre sans eux et les tests peuvent stubber `generer_embedding`.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

from ...config import DOCUMENTS_DIR, GEMINI_API_KEY, INDEX_DIR, MODELE_EMBEDDING, RAG_K
from . import chunks as chunks_mod
from .extraction import extraire_texte

_CLIENT_CACHE: Any = None


def _client() -> Any:
    """Client Gemini (création paresseuse + mise en cache au module)."""
    global _CLIENT_CACHE
    if _CLIENT_CACHE is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY manquante pour les embeddings")
        from google import genai

        _CLIENT_CACHE = genai.Client(api_key=GEMINI_API_KEY)
    return _CLIENT_CACHE


def generer_embedding(texte: str) -> list[float]:
    """Vecteur Gemini du texte (utilisé seul par la recherche et l'index)."""
    resultat = _client().models.embed_content(model=MODELE_EMBEDDING, contents=texte)
    return resultat.embeddings[0].values


def construire_index(chunks: list[dict]):
    """Construit l'index FAISS (IP, cosinus normalisé) des chunks."""
    import faiss
    import numpy as np

    vecteurs = [generer_embedding(c["texte"]) for c in chunks]
    matrice = np.array(vecteurs, dtype="float32")
    faiss.normalize_L2(matrice)

    index = faiss.IndexFlatIP(matrice.shape[1])
    index.add(matrice)
    return index


def sauvegarder_index(index, chunks: list[dict]) -> None:
    """Persiste l'index FAISS + les métadonnées des chunks dans INDEX_DIR."""
    import faiss

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_DIR / "index.faiss"))
    with open(INDEX_DIR / "chunks.pkl", "wb") as f:
        pickle.dump(chunks, f)


def charger_index() -> tuple[Any, list[dict]]:
    """Recharge (index FAISS, chunks) depuis INDEX_DIR. Lève FileNotFoundError."""
    import faiss

    index = faiss.read_index(str(INDEX_DIR / "index.faiss"))
    with open(INDEX_DIR / "chunks.pkl", "rb") as f:
        chunks = pickle.load(f)
    # Les index hérités du projet « AI » n'avaient pas de champ `source`.
    for c in chunks:
        c.setdefault("source", "(documents archivés)")
    return index, chunks


def index_existe() -> bool:
    return (INDEX_DIR / "index.faiss").is_file() and (INDEX_DIR / "chunks.pkl").is_file()


def etat_index() -> dict[str, Any]:
    """Résumé pour l'API : index présent ? combien de chunks/documents ?"""
    if not index_existe():
        return {"existe": False, "nb_chunks": 0, "documents": []}
    try:
        _, chunks = charger_index()
    except Exception:  # noqa: BLE001 — index corrompu ≠ crash API
        return {"existe": True, "nb_chunks": 0, "documents": []}
    sources = sorted({str(c.get("source") or "?") for c in chunks})
    return {"existe": True, "nb_chunks": len(chunks), "documents": sources}


def construire_index_documents() -> dict[str, Any]:
    """Ré-indexe tous les documents de DOCUMENTS_DIR (extraction + embedding).

    Appelée par POST /api/documents/reindex. Retourne un résumé
    {nb_documents, nb_chunks, duree_s}.
    """
    fichiers = sorted(DOCUMENTS_DIR.glob("*"))
    chunks: list[dict] = []
    documents_ok: list[str] = []
    echecs: list[str] = []

    for chemin in fichiers:
        if not chemin.is_file() or chemin.name.startswith("."):
            continue
        try:
            pages = extraire_texte(chemin)
        except (FileNotFoundError, ValueError) as exc:
            echecs.append(f"{chemin.name} ({exc})")
            continue
        chunks.extend(chunks_mod.decouper_en_chunks(pages, source=chemin.name))
        documents_ok.append(chemin.name)

    if not chunks:
        raise ValueError(
            "Aucun texte indexable dans le dossier documents (fichiers supportés : "
            "pdf, txt, md)."
        )

    index = construire_index(chunks)
    sauvegarder_index(index, chunks)
    return {
        "nb_documents": len(documents_ok),
        "documents": documents_ok,
        "nb_chunks": len(chunks),
        "echecs": echecs,
    }


def rechercher(question: str, k: int | None = None) -> list[dict]:
    """Retourne les K chunks les plus proches de *question* (vide si pas d'index).

    Chaque résultat : {source, page, texte, score}. Échecs de chargement ou
    d'embedding => liste vide (le chat continue sans contexte documentaire).
    """
    if not index_existe():
        return []
    try:
        index, chunks = charger_index()
    except Exception:  # noqa: BLE001 — jamais bloquant pour le chat
        return []

    k = k or RAG_K
    try:
        import faiss
        import numpy as np

        vecteur = np.array([generer_embedding(question)], dtype="float32")
        faiss.normalize_L2(vecteur)
        scores, indices = index.search(vecteur, k)
    except Exception:  # noqa: BLE001 — jamais bloquant pour le chat
        return []

    resultats = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        resultats.append({**chunks[idx], "score": float(score)})
    return resultats