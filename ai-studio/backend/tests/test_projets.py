"""Tests de l'API projets : import, arborescence, fichiers, recherche.

Exécutables sans réseau ni serveur (uniquement le disco/archive) :
     python tests/test_projets.py
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import pathlib
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import projets as ps  # noqa: E402
from app.services import registre as _reg  # noqa: E402
from app.services import workspace as _ws  # noqa: E402
from app.services.workspace import CheminHorsProjet  # noqa: E402


@contextlib.contextmanager
def _projets_isoles():
    """Isolate PROJETS_DIR (projets + workspace) + le registre dans un dossier
    temporaire : aucun état réel (data/registre.json) ne fuit dans les tests."""
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp) / "projets"
        ps.PROJETS_DIR = racine
        _ws.PROJETS_DIR = racine
        ancien_registre = _reg._FICHIER
        _reg._FICHIER = pathlib.Path(tmp) / "registre.json"
        try:
            yield racine
        finally:
            _reg._FICHIER = ancien_registre


def _sha(contenu: str) -> str:
    return hashlib.sha1(contenu.encode("utf-8")).hexdigest()


def _zip(mini: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in mini.items():
            z.writestr(nom, contenu)
    return buf.getvalue()


def _importer(mini: dict[str, str], nom: str | None = None) -> dict:
    return ps.importer_archive(_zip(mini), nom)


def test_import_zip_basique():
    with _projets_isoles():
        res = _importer({"a.txt": "bonjour", "sous/b.txt": "monde"}, "monprojet")
        assert res == {"id": "monprojet", "nom": "monprojet", "nb_fichiers": 2}
        assert ps.lire_fichier("monprojet", "a.txt")["contenu"] == "bonjour"
        assert ps.lire_fichier("monprojet", "sous/b.txt")["contenu"] == "monde"


def test_import_ignore_noeuds_lourds():
    with _projets_isoles():
        res = _importer({
            "src/main.py": "x=1",
            "node_modules/big/index.js": "y",
            "dist/bundle.js": "z",
            ".git/config": "c",
        }, "app")
        assert res["nb_fichiers"] == 1
        # archive à dossier unique "src/" -> aplatissement de la racine
        assert ["main.py"] == _ws.lister_arborescence(_ws.projet_existant("app"))


def test_import_remplace_projet_existant():
    with _projets_isoles():
        _importer({"vieux.txt": "1"}, "t")
        res = _importer({"neuf.txt": "2"}, "t")
        assert res["nb_fichiers"] == 1
        assert sorted(p.name for p in _ws.projet_existant("t").iterdir()) == ["neuf.txt"]


def test_import_archive_non_supportee():
    with _projets_isoles():
        try:
            ps.importer_archive(b"GIF89a notanarchive", "x")
        except ValueError:
            pass
        else:
            raise AssertionError("l'archive invalide aurait dû lever ValueError")


def test_slug_du_nom():
    assert ps._slug("   Mon Projet (v2)!  ") == "Mon-Projet-v2"
    assert ps._slug("") == "projet"


def test_lister_projets():
    with _projets_isoles():
        _ws.PROJETS_DIR.mkdir(parents=True)
        (_ws.PROJETS_DIR / "b").mkdir()
        (_ws.PROJETS_DIR / "a").mkdir()
        (_ws.PROJETS_DIR / ".cache").mkdir()
        (_ws.PROJETS_DIR / "a" / "f.py").write_text("k", encoding="utf-8")
        ids = [p["id"] for p in ps.lister_projets()]
        assert ids == ["a", "b"]
        assert ps.lister_projets()[0]["nb_fichiers"] == 1


def test_arborescence_imbriquee():
    with _projets_isoles():
        _importer({"z.txt": "1", "aa/sub/file.py": "2", "node_modules/x.js": "3"}, "p")
        racine = ps.arborescence("p")
        assert racine[0]["type"] == "dossier"
        assert racine[0]["nom"] == "aa"
        assert racine[0]["enfants"][0]["type"] == "dossier"
        assert racine[0]["enfants"][0]["nom"] == "sub"
        assert racine[0]["enfants"][0]["enfants"][0]["chemin"] == "aa/sub/file.py"
        noms = [e["nom"] for e in racine]
        assert noms == ["aa", "z.txt"]


def test_lire_fichier_manquant():
    with _projets_isoles():
        _importer({"a.txt": "x"}, "p")
        try:
            ps.lire_fichier("p", "nope.txt")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("fichier absent aurait dû lever FileNotFoundError")


def test_chemin_hors_projet():
    with _projets_isoles():
        _importer({"a.txt": "x"}, "p")
        for vilain in ("../secret.txt", "..\\secret.txt", "a/../../etc/passwd"):
            try:
                ps.lire_fichier("p", vilain)
            except CheminHorsProjet:
                pass
            else:
                raise AssertionError(f"{vilain!r} aurait dû être refusé")
            try:
                ps.ecrire_fichier("p", vilain, "0")
            except CheminHorsProjet:
                pass
            else:
                raise AssertionError(f"{vilain!r} aurait dû être refusé (écriture)")


def test_lire_binaire_rejete():
    with _projets_isoles():
        _importer({"img.jpg": "nimporte quoi mais binaire par ext"}, "p")
        try:
            ps.lire_fichier("p", "img.jpg")
        except ValueError:
            pass
        else:
            raise AssertionError("fichier binaire aurait dû être rejeté")


def test_ecrire_et_lire_fichier():
    with _projets_isoles():
        _importer({"a.py": "print(1)\n"}, "p")
        res = ps.ecrire_fichier("p", "src/mod.py", "def f():\n    return 42\n")
        assert res["chemin"] == "src/mod.py" and len(res["nouveau_sha"]) == 40
        lu = ps.lire_fichier("p", "src/mod.py")
        assert lu["langue"] == "python"
        assert lu["contenu"] == "def f():\n    return 42\n"
        # nouveau_sha stable/provenant du contenu
        res2 = ps.ecrire_fichier("p", "src/mod.py", "def f():\n    return 42\n")
        assert res2["nouveau_sha"] == res["nouveau_sha"]
        # écriture d'un nom de dossier existant -> erreur
        try:
            ps.ecrire_fichier("p", "src", "pouet")
        except IsADirectoryError:
            pass
        else:
            raise AssertionError("écrire sur un dossier aurait dû lever IsADirectoryError")


def test_recherche_insensible_casse_avec_contexte():
    with _projets_isoles():
        _importer({
            "a.txt": "ligne 0\nHello Monde\nligne 2",
            "b.py": "print('salut hello')\n",
        }, "p")
        res = ps.rechercher("p", "HELLO")
        assert res["nb_fichiers"] == 2
        corr_a = next(f["correspondances"] for f in res["fichiers"] if f["fichier"] == "a.txt")
        assert corr_a[0]["ligne"] == 2 and "hello monde" in corr_a[0]["extrait"].lower()
        assert corr_a[0]["num"] == 1


def test_recherche_vide_et_limite():
    with _projets_isoles():
        _importer({"a.txt": "rien ici"}, "p")
        assert ps.rechercher("p", "")["nb_fichiers"] == 0
        assert ps.rechercher("p", "  ")["nb_fichiers"] == 0
        assert ps.rechercher("p", "absent")["nb_fichiers"] == 0


def test_etat_fichiers_sha_et_absents():
    with _projets_isoles():
        _importer({"a.txt": "hello"}, "p")
        etat = ps.etat_fichiers("p", ["a.txt", "absent.txt"])
        assert "a.txt" in etat["fichiers"] and len(etat["fichiers"]["a.txt"]) == 40
        assert etat["fichiers"]["absent.txt"] is None


def test_etat_fichiers_tout_le_projet():
    with _projets_isoles():
        _importer({"a.txt": "1", "b.txt": "2"}, "p")
        ensemble = ps.etat_fichiers("p")
        assert set(ensemble["fichiers"]) == {"a.txt", "b.txt"}


def test_etat_projet_inconnu_renvoie_404_pas_500():
    """Régression : `GET /{projet}/etat` laissait remonter FileNotFoundError,
    que FastAPI rendait en 500 « Internal Server Error ». Le frontend polling
    ce endpoint sur un projet supprimé entre-temps interpretait ce 500 comme
    une panne du serveur au lieu d'un projet absent."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.projets import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    with _projets_isoles():
        _importer({"a.txt": "hello"}, "p")
        r = client.get("/api/projects/p/etat")
        assert r.status_code == 200, r.text
        assert r.json()["fichiers"]["a.txt"] == _sha("hello")

        # Projet absent (supprimé entre deux polls) : 404 attendu, jamais 500.
        r = client.get("/api/projects/inexistant/etat")
        assert r.status_code == 404, r.text
        assert r.status_code != 500


def test_etat_ignore_les_chemins_hors_projet():
    """Choix de conception du service : un chemin hors projet est ignoré, pas
    une erreur (le poll reste tolérant). Rien n'est lu hors du projet."""
    with _projets_isoles():
        _importer({"a.txt": "hello"}, "p")
        assert ps.etat_fichiers("p", ["../../evasion.txt"]) == {"projet": "p", "fichiers": {}}
        assert ps.etat_fichiers("p", ["a.txt"]) == {
            "projet": "p", "fichiers": {"a.txt": _sha("hello")}
        }


class _PathInaccessible(pathlib.Path):
    """`Path` dont `stat()` échoue sur un nom donné, comme un socket Docker.

    Reproduit le WinError 1920 réellement observé sous Windows sans
    monkeypatch global : `rglob` conserve le type de la racine, donc les
    enfants lèvent aussi.
    """

    CIBLE = "mysql.sock"

    def stat(self, *args, **kwargs):
        if self.name == self.CIBLE:
            raise OSError(1920, "Le système ne peut pas accéder au fichier")
        return super().stat(*args, **kwargs)


def test_arborescence_ignore_les_entrees_inaccessibles():
    """Une entrée illisible ne doit pas casser le parcours du projet.

    Ouvrir un dossier en mode direct peut viser n'importe quel dossier de
    travail. `p.stat()` non protégé sur un socket Docker faisait répondre 500
    à TOUTE l'arborescence, donc l'éditeur devenait inutilisable.

    Limite du test : la sous-classe ne couvre que l'appel `Path.stat()`, celui
    de `projets._noeuds` — la ligne exacte du traceback. `Path.is_file()`
    passe par `os.stat` et n'est pas simulé ici.
    """
    racine = _PathInaccessible(tempfile.mkdtemp())
    (racine / "src").mkdir()
    (racine / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (racine / "lisible.txt").write_text("ok\n", encoding="utf-8")
    (racine / "db-data").mkdir()
    (racine / "db-data" / "mysql.sock").write_bytes(b"")

    flt = ps.filtres_pour(racine)
    arbre = ps._noeuds(racine, racine, flt)
    noms = sorted(e["nom"] for e in arbre)

    assert noms == ["db-data", "lisible.txt", "src"], noms

    # Le socket est illisible : le dossier est listé mais vide, pas fatal.
    db_data = next(e for e in arbre if e["nom"] == "db-data")
    assert db_data["enfants"] == [], db_data

    # Les fichiers lisibles sont bien remontés, taille comprise. La taille est
    # relue sur disque : `write_text` traduit `\n` en `\r\n` sous Windows.
    src = next(e for e in arbre if e["nom"] == "src")
    assert src["enfants"][0]["chemin"] == "src/main.py"
    assert src["enfants"][0]["taille"] == (racine / "src" / "main.py").stat().st_size


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