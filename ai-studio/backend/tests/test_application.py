"""Tests d'application.py + agent_chat.py — exécutables sans dépendance réseau.

Lancement direct :  python tests/test_application.py
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_chat, application, workspace  # noqa: E402
from app.services.diff import build_diff  # noqa: E402
from app.services.workspace import CheminHorsProjet  # noqa: E402


def _sha(contenu: str) -> str:
    return hashlib.sha1(contenu.encode("utf-8")).hexdigest()


def _ecrire(tmp: str, rel: str, contenu: str) -> pathlib.Path:
    # Python 3.14 : le contexte `with TemporaryDirectory() as tmp` fournit
    # directement le chemin (str), pas l'objet.
    racine = pathlib.Path(tmp)
    p = workspace.chemin_securise(racine, rel)
    workspace.ecrire_fichier(p, contenu)
    return p


# ────────────────────────────────────────────────────────────────────────────

def test_applique_tous_les_hunks():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        ancien = "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\n"
        _ecrire(tmp, "f.txt", ancien)
        nouveau = "l1\nX\nl3\nl4\nl5\nl6\nl7\nY\nZ\nl10\n"
        diff = build_diff(ancien, nouveau, contexte=1)

        resultats = application.appliquer_changements(racine, [
            {"fichier": "f.txt", "action": "write",
             "source_hash": diff["source_hash"], "acceptes": diff["hunks"]}
        ])
        assert resultats[0].statut == "ok"
        assert workspace.lire_fichier_ou(racine / "f.txt") == nouveau
        assert resultats[0].nouveau_sha == _sha(nouveau)


def test_applique_un_seul_hunk():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        ancien = "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\nl11\nl12\n"
        _ecrire(tmp, "f.txt", ancien)
        nouveau = "l1\nX\nl3\nl4\nl5\nl6\nl7\nY\nZ\nl10\nl11\nl12\n"
        diff = build_diff(ancien, nouveau, contexte=1)
        assert diff["stats"]["nb_hunks"] == 2

        resultats = application.appliquer_changements(racine, [
            {"fichier": "f.txt", "action": "write",
             "source_hash": diff["source_hash"], "acceptes": [diff["hunks"][1]]}
        ])
        assert resultats[0].statut == "ok"
        attendu = "l1\nl2\nl3\nl4\nl5\nl6\nl7\nY\nZ\nl10\nl11\nl12\n"
        assert workspace.lire_fichier_ou(racine / "f.txt") == attendu


def test_aucun_hunk_pas_modifie():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        _ecrire(tmp, "f.txt", "avant\n")
        resultats = application.appliquer_changements(racine, [
            {"fichier": "f.txt", "action": "write", "source_hash": "x", "acceptes": []}
        ])
        assert resultats[0].statut == "pas_modifie"
        assert workspace.lire_fichier_ou(racine / "f.txt") == "avant\n"


def test_fichier_perime():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        _ecrire(tmp, "f.txt", "avant\n")
        diff = build_diff("avant\n", "modifie\n")
        # le fichier bouge entre la proposition et l'application
        _ecrire(tmp, "f.txt", "quelquun dautre\n")

        resultats = application.appliquer_changements(racine, [
            {"fichier": "f.txt", "action": "write",
             "source_hash": diff["source_hash"], "acceptes": diff["hunks"]}
        ])
        assert resultats[0].statut == "erreur"
        assert "périmé" in (resultats[0].message or "")
        # le contenu externe n'est pas écrasé
        assert workspace.lire_fichier_ou(racine / "f.txt") == "quelquun dautre\n"


def test_action_suppression():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        _ecrire(tmp, "a.txt", "contenu\n")
        resultats = application.appliquer_changements(racine, [
            {"fichier": "a.txt", "action": "delete"}
        ])
        assert resultats[0].statut == "ok"
        assert not (racine / "a.txt").exists()


def test_creation_de_fichier():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        nouveau = "ligne1\nligne2\n"
        diff = build_diff("", nouveau)
        resultats = application.appliquer_changements(racine, [
            {"fichier": "nouveau/fichier.py", "action": "write",
             "source_hash": diff["source_hash"], "acceptes": diff["hunks"]}
        ])
        assert resultats[0].statut == "ok"
        assert workspace.lire_fichier_ou(racine / "nouveau/fichier.py") == nouveau


def test_chemin_hors_projet():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        try:
            application.appliquer_changements(racine, [
                {"fichier": "../evil.txt", "action": "write", "acceptes": []}
            ])
        except CheminHorsProjet:
            return
        raise AssertionError("CheminHorsProjet attendue")


def test_creer_propositions_depuis_simulation():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        _ecrire(tmp, "src/app.py", "print('ancien')\n")
        props = agent_chat.creer_propositions(racine, [
            {"chemin": "src/app.py", "contenu": "print('nouveau')\n"},
            {"chemin": "nouveau.txt", "contenu": "hello\n"},
            {"chemin": "src/app.py", "contenu": "print('ancien')\n"},  # identique : ignoré
        ])
        assert len(props) == 2
        for p in props:
            assert p["action"] == "write"
            assert p["source_hash"]
            assert isinstance(p["hunks"], list) and p["hunks"]
            assert p["stats"]["nb_hunks"] >= 1

        # Le flux complet simulation -> hunks -> application doit rester cohérent.
        modifications = [
            {"fichier": p["fichier"], "action": p["action"],
             "source_hash": p["source_hash"], "acceptes": p["hunks"]}
            for p in props
        ]
        resultats = application.appliquer_changements(racine, modifications)
        assert all(r.statut == "ok" for r in resultats)
        assert workspace.lire_fichier_ou(racine / "src/app.py") == "print('nouveau')\n"
        assert workspace.lire_fichier_ou(racine / "nouveau.txt") == "hello\n"


# ────────────────────────────────────────────────────────────────────────────

def _tout_executer():
    tests = [
        (nom, obj)
        for nom, obj in sorted(globals().items())
        if nom.startswith("test_") and callable(obj)
    ]
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