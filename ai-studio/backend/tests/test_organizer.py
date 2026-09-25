"""Tests du Smart File Organizer (port business) — isolés et hors réseau.

     python tests/test_organizer.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.business.smart_file_organizer import config as org_config  # noqa: E402
from app.business.smart_file_organizer import db as org_db  # noqa: E402
from app.business.smart_file_organizer import scanner  # noqa: E402
from app.business.smart_file_organizer.organizer import (  # noqa: E402
    OrganizerErreur,
    analyser,
    organiser,
    valider_racine,
)


def _situation(tmp: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Crée un dossier source avec 2 fichiers texte + 1 image. Retourne (src, root)."""
    root = pathlib.Path(tmp)
    src = root / "telechargements"
    (src / "divers").mkdir(parents=True)
    (src / "note.txt").write_text("bonjour", encoding="utf-8")
    (src / "divers" / "rapport.txt").write_text("rapport", encoding="utf-8")
    (src / "photo.png").write_bytes(b"\x89PNG confirm")
    return src, root


def test_categorise_extensions():
    assert scanner.categorize_file(pathlib.Path("r/a.pdf")) == ("Documents", ".pdf")
    assert scanner.categorize_file(pathlib.Path("r/p.png")) == ("Images", ".png")
    assert scanner.categorize_file(pathlib.Path("r/prog.py")) == ("Programmes", ".py")
    assert scanner.categorize_file(pathlib.Path("r/inconnu.xyz")) == ("Divers", ".xyz")
    assert scanner.categorize_file(pathlib.Path("r/a.tar.gz")) == ("Archives", ".tar.gz")


def test_list_files_ignore_git_et_sortie():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("x", encoding="utf-8")
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "organized_files").mkdir()
        (root / "organized_files" / "deja.txt").write_text("déjà", encoding="utf-8")
        fichiers = scanner.list_files(root)
        assert [f.name for f in fichiers] == ["a.txt"]


def test_analyser_non_destructif():
    with tempfile.TemporaryDirectory() as tmp:
        src, root = _situation(tmp)
        avant = sorted(p.name for p in src.rglob("*") if p.is_file())
        rapport = analyser(src)
        apres = sorted(p.name for p in src.rglob("*") if p.is_file())
        assert avant == apres, "l'analyse ne doit rien déplacer"
        assert rapport["nb_fichiers"] == 3
        cats = {c["categorie"]: len(c["fichiers"]) for c in rapport["par_categorie"]}
        assert cats == {"Documents": 2, "Images": 1}
        assert rapport["sortie"].endswith("organized_files")
        assert (src / "note.txt").exists()


def test_organiser_deplace_par_categorie():
    with tempfile.TemporaryDirectory() as tmp:
        src, root = _situation(tmp)
        resultat = organiser(src)
        assert resultat["deplaces"] == 3
        assert resultat["par_categorie"] == {"Documents": 2, "Images": 1}
        sortie = root / "organized_files"
        assert sorted(p.name for p in (sortie / "Documents").iterdir()) == [
            "note.txt", "rapport.txt"]
        assert (sortie / "Images" / "photo.png").exists()
        assert not (src / "note.txt").exists()


def test_organiser_gere_les_collisions():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        src = root / "src"
        (src / "a").mkdir(parents=True)
        (src / "b").mkdir()
        (src / "a" / "doc.txt").write_text("1", encoding="utf-8")
        (src / "b" / "doc.txt").write_text("2", encoding="utf-8")
        resultat = organiser(src)
        assert resultat["deplaces"] == 2
        doss = root / "organized_files" / "Documents"
        noms = sorted(p.name for p in doss.iterdir())
        assert noms == ["doc (2).txt", "doc.txt"]
        assert (doss / "doc.txt").read_text(encoding="utf-8") == "1"
        assert (doss / "doc (2).txt").read_text(encoding="utf-8") == "2"


def test_refus_dossier_git():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / ".git").mkdir()
        try:
            valider_racine(root)
        except OrganizerErreur:
            pass
        else:
            raise AssertionError("dossier git aurait dû être refusé")


def test_refus_espace_projets():
    with tempfile.TemporaryDirectory() as tmp:
        join_interdit = [pathlib.Path(tmp) / "projets"]
        ancien = org_config.REPERTOIRES_INTERDITS
        org_config.REPERTOIRES_INTERDITS = join_interdit
        try:
            projet = pathlib.Path(tmp) / "projets" / "demo"
            projet.mkdir(parents=True)
            try:
                valider_racine(projet)
            except OrganizerErreur:
                pass
            else:
                raise AssertionError("l'espace projets aurait dû être refusé")
        finally:
            org_config.REPERTOIRES_INTERDITS = ancien


def test_refus_dossier_inexistant():
    try:
        valider_racine("/chemin/qui/n/existe/pas/xyz")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("le dossier inexistant aurait dû lever FileNotFoundError")


def test_db_historique_et_stats():
    with tempfile.TemporaryDirectory() as tmp:
        org_db.DB_PATH = pathlib.Path(tmp) / "organizer.db"
        org_db.init_db()
        org_db.update_stats(3, 1500)
        assert org_db.fetch_stats() == (3, 1500)
        src = pathlib.Path(tmp) / "s"
        dst = pathlib.Path(tmp) / "d"
        org_db.log_move(src, dst)
        hist = org_db.fetch_history(5)
        assert len(hist) == 1 and hist[0]["src_path"] == str(src)


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