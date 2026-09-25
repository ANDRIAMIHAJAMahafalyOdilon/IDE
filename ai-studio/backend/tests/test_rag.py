"""Tests du module RAG (extraction, chunking, index FAISS) + API documents.

Exécutables sans réseau et sans clé LLM : `generer_embedding` est stubé par
une fonction déterministe locale et les dossiers de documents/index sont
redirigés vers des répertoires temporaires.

     python tests/test_rag.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from fastapi.testclient import TestClient  # noqa: E402

from app.api import documents as documents_api  # noqa: E402
from app.main import app  # noqa: E402
from app.services.rag import embeddings as rag_emb  # noqa: E402
from app.services.rag.chunks import decouper_en_chunks  # noqa: E402
from app.services.rag.extraction import extraire_texte  # noqa: E402
from app.services.rag.web import rechercher as rechercher_web  # noqa: E402


def _faux_embedding(texte: str) -> list[float]:
    """Vecteur 8D déterministe (approx. sémantique par caractères)."""
    vect = [0.0] * 8
    for i, c in enumerate(texte):
        vect[i % 8] += ord(c)
    norme = sum(v * v for v in vect) ** 0.5 or 1.0
    return [v / norme for v in vect]


class _RepertoiresTemp:
    """Point les dossiers RAG vers un tmp et stub l'embedding."""

    def __init__(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="proxy_test_"))
        self.ancien_docs = rag_emb.DOCUMENTS_DIR
        self.ancien_index = rag_emb.INDEX_DIR
        self.ancien_embed = rag_emb.generer_embedding
        self.ancien_api_docs = documents_api.DOCUMENTS_DIR

    def __enter__(self):
        rag_emb.DOCUMENTS_DIR = self.tmp / "documents"
        rag_emb.INDEX_DIR = self.tmp / "index"
        documents_api.DOCUMENTS_DIR = rag_emb.DOCUMENTS_DIR
        rag_emb.DOCUMENTS_DIR.mkdir(exist_ok=True)
        rag_emb.INDEX_DIR.mkdir(exist_ok=True)
        rag_emb.generer_embedding = _faux_embedding
        return self

    def __exit__(self, *exc) -> None:
        rag_emb.DOCUMENTS_DIR = self.ancien_docs
        rag_emb.INDEX_DIR = self.ancien_index
        documents_api.DOCUMENTS_DIR = self.ancien_api_docs
        rag_emb.generer_embedding = self.ancien_embed


# ─────────────────────────── Extraction (hors réseau) ─────────────────────

def test_extraire_txt_et_md():
    with tempfile.TemporaryDirectory() as tmp:
        chemin_txt = pathlib.Path(tmp) / "notes.txt"
        chemin_md = pathlib.Path(tmp) / "cours.md"
        chemin_txt.write_text("   Hello le cours  ", encoding="utf-8")
        chemin_md.write_text("# Titre\n\nContenu de cours multilingue.", encoding="utf-8")

        pages_txt = extraire_texte(chemin_txt)
        assert len(pages_txt) == 1
        assert pages_txt[0]["page"] == 1
        assert pages_txt[0]["texte"].strip() == "Hello le cours"

        pages_md = extraire_texte(chemin_md)
        assert len(pages_md) == 1
        assert "Contenu de cours" in pages_md[0]["texte"]


def test_extraire_pdf_inconnu_et_fichier_absent():
    with tempfile.TemporaryDirectory() as tmp:
        inconnu = pathlib.Path(tmp) / "note.docx"
        inconnu.write_bytes(b"x")
        try:
            extraire_texte(inconnu)
            raise AssertionError("devrait lever ValueError")
        except ValueError:
            pass

        try:
            extraire_texte(pathlib.Path(tmp) / "absent.pdf")
            raise AssertionError("devrait lever FileNotFoundError")
        except FileNotFoundError:
            pass


# ─────────────────────────── Découpage en chunks ─────────────────────────

def test_decouper_chunks_taille_et_chevauchement():
    pages = [{"page": 1, "texte": " ".join(["mot"] * 450)}]
    chunks = decouper_en_chunks(pages, source="cours.txt")

    assert len(chunks) == 3  # 200 puis 170 puis 120 mots
    assert all(c["source"] == "cours.txt" for c in chunks)
    assert all(c["page"] == 1 for c in chunks)
    assert all(len(c["texte"].split()) <= 200 for c in chunks)
    # chevauchement : la fin du chunk 0 se retrouve au début du chunk 1
    queue_slice = " ".join(chunks[0]["texte"].split()[-30:])
    assert queue_slice == " ".join(chunks[1]["texte"].split()[:30])


def test_decouper_page_vide_ignoree():
    chunks = decouper_en_chunks(
        [{"page": 1, "texte": ""}, {"page": 2, "texte": " \n "}],
        source="cours.md",
    )
    assert chunks == []


# ─────────────────── Index FAISS (embedding stubé) ───────────────────────

def test_index_execution_complete_sur_fichiers():
    with _RepertoiresTemp() as env:
        (env.tmp / "documents" / "alpha.txt").write_text(
            "La photosynthèse convertit l'énergie."
        )
        (env.tmp / "documents" / "beta.md").write_text(
            "Les équations de Maxwell décrivent le phénomène."
        )

        resume = rag_emb.construire_index_documents()
        assert resume["nb_documents"] == 2
        assert resume["nb_chunks"] >= 2

        etat = rag_emb.etat_index()
        assert etat["existe"] is True
        assert etat["nb_chunks"] == resume["nb_chunks"]
        assert set(etat["documents"]) == {"alpha.txt", "beta.md"}

        resultats = rag_emb.rechercher("photosynthèse", k=2)
        assert len(resultats) >= 1
        assert resultats[0]["source"] in {"alpha.txt", "beta.md"}
        assert "page" in resultats[0]
        assert "score" in resultats[0]
        assert len(resultats[0]["texte"]) > 0


def test_rechercher_sans_index_retourne_vide():
    with _RepertoiresTemp() as env:
        assert not rag_emb.index_existe()
        assert rag_emb.rechercher("quoi que ce soit") == []
        assert rag_emb.etat_index()["existe"] is False


# ───────────────────────────── API documents ─────────────────────────────

def test_api_documents_upload_reindex_liste_delete():
    client = TestClient(app)
    with _RepertoiresTemp() as env:
        # liste vide au départ
        rep = client.get("/api/documents")
        assert rep.status_code == 200
        assert rep.json()["documents"] == []

        # upload d'un cours texte
        contenu = "La géométrie euclidienne repose sur cinq axiomes."
        rep = client.post(
            "/api/documents/upload",
            files={"fichier": ("geometrie.txt", contenu, "text/plain")},
        )
        assert rep.status_code == 200
        assert rep.json()["nom"] == "geometrie.txt"

        # upload d'un format refusé
        rep = client.post(
            "/api/documents/upload",
            files={"fichier": ("virus.exe", b"MZ", "application/octet-stream")},
        )
        assert rep.status_code == 400

        # liste : 1 document
        rep = client.get("/api/documents")
        docs = rep.json()["documents"]
        assert len(docs) == 1 and docs[0]["nom"] == "geometrie.txt"

        # reindex (embedding stubé → aucune requête réseau)
        rep = client.post("/api/documents/reindex")
        assert rep.status_code == 200
        assert rep.json()["nb_documents"] == 1
        assert rep.json()["nb_chunks"] >= 1

        # l'index est visible
        rep = client.get("/api/documents")
        assert rep.json()["index"]["existe"] is True
        assert rep.json()["index"]["documents"] == ["geometrie.txt"]

        # suppression
        rep = client.delete("/api/documents/geometrie.txt")
        assert rep.status_code == 200
        rep = client.delete("/api/documents/geometrie.txt")
        assert rep.status_code == 404


# ─────────────────────────────── Recherche web ───────────────────────────

def test_recherche_web_brute():
    """Sans réseau, ddgs lève une exception : `rechercher` doît propager
    (le chat l'attrape en amont pour ne pas casser le streaming)."""
    from app.services.rag.web import rechercher as rec

    try:
        resultats = rec("test hors-ligne", max_resultats=1)
    except Exception:  # noqa: BLE001 — pas de réseau en CI
        return  # comportement accepté (le chat ignore l'échec)
    assert isinstance(resultats, list)


def _tout_executer():
    tests = sorted(
        (nom, obj) for nom, obj in globals().items()
        if nom.startswith("test_") and callable(obj)
    )
    nb_ok = 0
    for nom, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  [E] {nom} : {exc!r}")
        else:
            nb_ok += 1
            print(f"  [OK] {nom}")
    print(f"\n{nb_ok}/{len(tests)} tests réussis")
    return 0 if nb_ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_tout_executer())