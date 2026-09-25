"""Tests du mode « dossier direct » (ouverture VS Code-like d'un dossier local).

Covered : validation/refus, registre, aperçu filtré (.gitignore + dossiers
lourds), résolution via projet_existant, écriture réelle sur disque,
anti-traversal identique aux projets importés, gate du mode autonome.

Exécution :  python tests/test_dossier_local.py
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import projets as ps  # noqa: E402
from app.services import filtres  # noqa: E402
from app.services import registre  # noqa: E402
from app.services import workspace as _ws  # noqa: E402
from app.services.workspace import CheminHorsProjet  # noqa: E402


@contextlib.contextmanager
def _isole_registre(racine_projets: pathlib.Path):
    """Isole registre + PROJETS_DIR (et neutralise les refus de chemins système)."""
    tmp = racine_projets.parent
    anciens = (registre._FICHIER, registre.PROJETS_DIR,
               registre._racines_invalides, registre._chemins_refuses_meta,
               ps.PROJETS_DIR, _ws.PROJETS_DIR)
    registre._FICHIER = tmp / "registre.json"
    registre.PROJETS_DIR = racine_projets
    registre._racines_invalides = lambda: []
    registre._chemins_refuses_meta = lambda: [racine_projets.resolve()]
    ps.PROJETS_DIR = racine_projets
    _ws.PROJETS_DIR = racine_projets
    racine_projets.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        (registre._FICHIER, registre.PROJETS_DIR,
         registre._racines_invalides, registre._chemins_refuses_meta,
         ps.PROJETS_DIR, _ws.PROJETS_DIR) = anciens
        filtres._spec_pour.cache_clear()
        # mémoire du registre rechargée à chaque appel — aucun autre état.


def _projet_reel(root: pathlib.Path, gitignore: str = "") -> pathlib.Path:
    """Construit un faux projet local avec node_modules/ et .gitignore."""
    racine = root / "dossier-reel"
    (racine / "src").mkdir(parents=True)
    (racine / "src" / "main.py").write_text("print('bonjour')\n", encoding="utf-8")
    (racine / "README.md").write_text("# Projet\n", encoding="utf-8")
    (racine / "log").mkdir()
    (racine / "log" / "app.log").write_text("INFO x\n", encoding="utf-8")
    (racine / "coverage").mkdir()
    (racine / "coverage" / "index.html").write_text("<html></html>", encoding="utf-8")
    node = racine / "node_modules" / "pkg"
    node.mkdir(parents=True)
    (node / "index.js").write_text("module.exports={};\n", encoding="utf-8")
    if gitignore:
        (racine / ".gitignore").write_text(gitignore, encoding="utf-8")
    return racine


# ────────────────────────────── Validation ─────────────────────────────────

def test_refus_racine_de_lecteur():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        if os.name == "nt":
            try:
                registre.valider_chemin_local("C:\\")
            except ValueError:
                pass
            else:
                raise AssertionError("la racine C:\\ aurait dû être refusée")


def test_refus_chemin_non_absolu():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        for vilain in ("", "   ", "relative/projet"):
            try:
                registre.valider_chemin_local(vilain)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{vilain!r} aurait dû être refusé")


def test_refus_dossier_systeme():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp) / "projets"
        with _isole_registre(racine):
            registre._racines_invalides = lambda: [pathlib.Path(tmp).resolve()]
            try:
                registre.valider_chemin_local(tmp)
            except ValueError:
                pass
            else:
                raise AssertionError("un dossier \"système\" aurait dû être refusé")


def test_racines_invalides_excluent_les_racines_de_lecteur():
    """Régression : C:\\ dans _racines_invalides refusait TOUT chemin du disque
    (p.is_relative_to("C:\\") est toujours vrai)."""
    if os.name != "nt":
        return
    for racine in registre._racines_invalides():
        assert racine != pathlib.Path(racine.anchor).resolve(), (
            f"racine de lecteur incluse à tort dans les racines invalides : {racine}"
        )


def test_chemin_utilisateur_reel_accepte():
    """Régression : un vrai dossier utilisateur doit être ouvert sans patch."""
    if os.name != "nt":
        return
    dossier = pathlib.Path.home() / "_smoke_direct_check"
    dossier.mkdir(exist_ok=True)
    ancien_meta = registre._chemins_refuses_meta
    registre._chemins_refuses_meta = lambda: []  # sinon PROJECT_ROOT (le dépôt) bloque
    try:
        chemin, nom = registre.valider_chemin_local(str(dossier))
        assert chemin == dossier.resolve()
        assert nom == "_smoke_direct_check"
    finally:
        registre._chemins_refuses_meta = ancien_meta
        dossier.rmdir()


def test_refus_non_dossier():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        fichier = pathlib.Path(tmp) / "un_fichier.txt"
        fichier.write_text("x", encoding="utf-8")
        try:
            registre.valider_chemin_local(str(fichier))
        except ValueError:
            pass
        else:
            raise AssertionError("un fichier n'est pas un dossier : aurait dû être refusé")


# ─────────────────────────── Aperçu / filtres ───────────────────────────────

def test_gitignore_negation_et_ancrage():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "sub").mkdir()
        (root / ".gitignore").write_text(
            "*.log\n!important.log\n/racine.tmp\nsub/\n", encoding="utf-8"
        )
        flt = filtres.filtres_pour(root)
        assert flt.ignore("a.log") is True            # *.log
        assert flt.ignore("important.log") is False   # !important.log
        assert flt.ignore("racine.tmp") is True       # /racine.tmp (ancré)
        assert flt.ignore("autre/racine.tmp") is False  # ancrage = racine uniquement
        assert flt.ignore("sub", est_dossier=True) is True   # dossier sub/
        assert flt.ignore("sub/f.txt") is True        # contenu du dossier ignoré
        assert flt.ignore("node_modules", est_dossier=True) is True  # dur


def test_exclusions_dures_env_et_secrets_sans_gitignore():
    """`.env*` et secrets doivent être exclus DURS, même sans .gitignore."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        sensibles = (
            ".env", ".env.local", ".env.production", ".env.staging", ".envrc",
            ".npmrc", "id_rsa", "id_ed25519", "credentials.json",
            "server.pem", "cle.key", "cert.p12",
        )
        for nom in sensibles:
            (root / nom).write_text("SECRET=1\n", encoding="utf-8")
        # Modèles d'environnement : committés par convention -> VISIBLES.
        modeles = (".env.example", ".env.sample", ".env.template")
        for nom in modeles:
            (root / nom).write_text("API_KEY=\n", encoding="utf-8")
        (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (root / "environnement.md").write_text("pas un secret\n", encoding="utf-8")
        flt = filtres.filtres_pour(root)  # aucun .gitignore présent
        for nom in sensibles:
            assert flt.ignore(nom) is True, f"{nom} devrait être exclu dur"
        for nom in modeles:
            assert flt.ignore(nom) is False, f"{nom} (modèle) devrait rester visible"
        assert flt.ignore("app.py") is False
        assert flt.ignore("environnement.md") is False  # préfixe `.env` ≠ `.env`


def test_dot_dossiers_systemiques_absents_du_tree():
    """Régression `lstrip("./")` : les dot-dossiers systémiques (.git, .next…)
    doivent être exclus durs, ainsi que TOUS leurs fichiers, dans le tree."""
    dot_dossiers = (
        ".git", ".hg", ".svn", ".next", ".cache", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".idea", ".vscode", ".tox", ".gradle",
        ".venv", "node_modules",
    )
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        racine = pathlib.Path(tmp) / "projet-dotdirs"
        (racine / "src").mkdir(parents=True)
        (racine / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (racine / "README.md").write_text("# Projet\n", encoding="utf-8")
        for nom in dot_dossiers:
            interne = racine / nom / "sous"
            interne.mkdir(parents=True)
            (interne / "fichier.txt").write_text("bruit\n", encoding="utf-8")
            (racine / nom / "direct.txt").write_text("bruit\n", encoding="utf-8")

        # 1) au niveau du filtre : dossier ET fichiers internes ignorés
        flt = filtres.filtres_pour(racine)
        for nom in dot_dossiers:
            assert flt.ignore(nom, est_dossier=True) is True, nom
            assert flt.ignore(f"{nom}/direct.txt") is True, nom
            assert flt.ignore(f"{nom}/sous/fichier.txt") is True, nom

        # 2) bout en bout : rien de ces dossiers n'apparaît dans l'arborescence
        ps.ouvrir_dossier_local(str(racine))
        chemins: list[str] = []

        def _plat(noeuds):
            for e in noeuds:
                chemins.append(e["chemin"])
                if e["type"] == "dossier":
                    _plat(e.get("enfants", []))

        _plat(ps.arborescence("projet-dotdirs"))
        assert set(chemins) == {"src", "src/main.py", "README.md"}, chemins
        for nom in dot_dossiers:
            assert not any(c == nom or c.startswith(nom + "/") for c in chemins), nom

        # 3) l'état (SHA de tous les fichiers) ne les voit pas non plus
        etat = ps.etat_fichiers("projet-dotdirs")
        assert set(etat["fichiers"]) == {"src/main.py", "README.md"}, etat


def test_apercu_filtre_node_modules_et_gitignore():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        projet_reel = _projet_reel(
            pathlib.Path(tmp),
            gitignore="# log uniquement\nlog/\n*.tmp\n",
        )
        (projet_reel / "plan.tmp").write_text("z", encoding="utf-8")
        apercu = ps.apercu_dossier_local(str(projet_reel))
        assert apercu["nom"] == "dossier-reel"
        assert apercu["chemin"] == str(projet_reel.resolve())
        # node_modules/, coverage/ (dossiers lourds) et log/ + *.tmp (gitignore) exclus.
        # seul le .gitignore racine lui-même demeure visible.
        assert {e for e in apercu["exemples"]} == {"src/main.py", "README.md", ".gitignore"}
        assert apercu["nb_fichiers"] == 3


def test_dossier_open_local_alire_ecrire_sur_vrai_disque():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        projet_reel = _projet_reel(pathlib.Path(tmp))
        id_ = None
        for entrees in ps.lister_projets():
            if entrees["id"] == "dossier-reel":
                id_ = entrees["id"]
        assert id_ is None  # pas encore ouvert

        ouvre = ps.ouvrir_dossier_local(str(projet_reel))
        assert ouvre["origine"] == "dossier"
        assert ouvre["chemin"] == str(projet_reel.resolve())
        # main.py + README.md + log/app.log (node_modules / coverage exclus durs).
        assert ouvre["nb_fichiers"] == 3

        liste = [p for p in ps.lister_projets() if p["id"] == "dossier-reel"]
        assert liste and liste[0]["origine"] == "dossier"

        racine = _ws.projet_existant("dossier-reel")
        assert racine.resolve() == projet_reel.resolve()

        noms = [e["nom"] for e in ps.arborescence("dossier-reel")]
        assert noms == ["log", "src", "README.md"]  # dossiers d'abord, puis alpha

        # Écriture réelle dans le fichier du disque (pas une copie).
        res = ps.ecrire_fichier("dossier-reel", "src/main.py", "print('changé')\n")
        assert (projet_reel / "src" / "main.py").read_text(encoding="utf-8") == "print('changé')\n"
        assert res["nouveau_sha"]

        lu = ps.lire_fichier("dossier-reel", "src/main.py")
        assert lu["contenu"] == "print('changé')\n"

        corr = ps.rechercher("dossier-reel", "chang")
        assert corr["nb_fichiers"] == 1

        etat = ps.etat_fichiers("dossier-reel")
        assert set(etat["fichiers"]) == {"README.md", "src/main.py", "log/app.log"}


def test_antitraversal_identique_projet_direct():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        secret = pathlib.Path(tmp) / "secret.txt"
        secret.write_text("hors projet", encoding="utf-8")
        projet_reel = _projet_reel(pathlib.Path(tmp))
        ps.ouvrir_dossier_local(str(projet_reel))
        for vilain in ("../secret.txt", "..\\secret.txt", "src/../../../secret.txt"):
            try:
                ps.lire_fichier("dossier-reel", vilain)
            except CheminHorsProjet:
                pass
            else:
                raise AssertionError(f"{vilain!r} aurait dû être refusé (lecture)")
            try:
                ps.ecrire_fichier("dossier-reel", vilain, "0")
            except CheminHorsProjet:
                pass
            else:
                raise AssertionError(f"{vilain!r} aurait dû être refusé (écriture)")


def test_agente_mode_autonome_gate():
    with tempfile.TemporaryDirectory() as tmp, _isole_registre(pathlib.Path(tmp) / "projets"):
        projet_reel = _projet_reel(pathlib.Path(tmp))
        ps.ouvrir_dossier_local(str(projet_reel))
        assert registre.est_projet_direct("dossier-reel") is True
        assert registre.est_projet_direct("archive-quelconque") is False
        # le dossier devenu absent => plus résolu (FileNotFoundError propre)
        shutil.rmtree(projet_reel, ignore_errors=True)
        try:
            _ws.projet_existant("dossier-reel")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("dossier supprimé du disque : projet_existant aurait dû échouer")


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