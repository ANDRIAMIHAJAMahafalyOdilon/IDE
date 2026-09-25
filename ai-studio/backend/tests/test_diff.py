"""Tests unitaires de app.services.diff — exécutables sans dépendance.

Lancement direct :  python tests/test_diff.py
(compatible pytest : seules des fonctions `test_*` avec des `assert`.)
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services.diff import (  # noqa: E402
    ContenuStale,
    Hunk,
    HunksChevauchants,
    apply_hunk,
    apply_hunks,
    build_diff,
    TYPE_AJOUT,
    TYPE_SUPPRESSION,
)

# ────────────────────────────────────────────────────────────────────────────
# Données d'exemples
# ────────────────────────────────────────────────────────────────────────────

PROSE = "\n".join(f"l{i}" for i in range(1, 13)) + "\n"


def _exemple_2_hunks() -> tuple[str, str]:
    """Deux modifications espacées -> deux hunks (contexte=1)."""
    ancien = PROSE
    nouveau = (
        "l1\n"
        "L2\n"
        "l3\nl4\nl5\nl6\nl7\nl8\n"
        "l9a\nl9b\n"
        "l10\nl11\nl12\n"
    )
    return ancien, nouveau


POSSIBILITES: list[tuple[str, str]] = [
    # insertion au milieu
    ("a\nb\nc\n", "a\nINSERT\nb\nc\n"),
    # suppression au milieu
    ("a\nb\nc\n", "a\nc\n"),
    # remplacement local
    ("a\nb\nc\n", "a\nB2\nc\n"),
    # création de fichier (ancien vide)
    ("", "x\ny\nz\n"),
    # suppression totale (nouveau vide)
    ("x\ny\nz\n", ""),
    # saut de ligne \r\n
    ("a\r\nb\r\nc\r\n", "a\r\nMODIFIE\r\nc\r\n"),
    # pas de newline finale
    ("a\nb", "a\nbBIS"),
    # nouvelle ligne ajoutée à la fin
    ("a\nb\n", "a\nb\nc\n"),
    # deux modifications proches, contexte par défaut
    (PROSE, PROSE),
]


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────

def test_identiques_pas_de_hunk():
    res = build_diff("a\nb\nc\n", "a\nb\nc\n")
    assert res["modifie"] is False
    assert res["hunks"] == []
    assert res["stats"]["nb_hunks"] == 0


def test_round_trip_tous_hunks():
    for ancien, nouveau in POSSIBILITES:
        res = build_diff(ancien, nouveau)
        reconstruit = apply_hunks(ancien, res["hunks"], source_hash=res["source_hash"])
        assert reconstruit == nouveau, (
            f"échec: ancien={ancien!r} nouveau={nouveau!r} -> {reconstruit!r}"
        )


def test_aucun_hunk_garde_ancien():
    for ancien, nouveau in POSSIBILITES:
        res = build_diff(ancien, nouveau)
        assert apply_hunks(ancien, []) == ancien


def test_deux_hunks_independants():
    ancien, nouveau = _exemple_2_hunks()
    res = build_diff(ancien, nouveau, contexte=1)
    assert res["stats"]["nb_hunks"] == 2

    h0, h1 = res["hunks"]
    assert apply_hunks(ancien, [h0]) == "l1\nL2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10\nl11\nl12\n"
    assert apply_hunks(ancien, [h1]) == (
        "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9a\nl9b\nl10\nl11\nl12\n"
    )


def test_contexte_inclus():
    ancien, nouveau = _exemple_2_hunks()
    res = build_diff(ancien, nouveau, contexte=1)
    h0 = res["hunks"][0]
    types = [l["type"] for l in h0["lignes"]]
    contenus = [l["contenu"] for l in h0["lignes"]]
    # contexte 1 : la ligne l1 précède la suppression, l3 suit l'ajout
    assert types == ["context", "del", "add", "context"]
    assert contenus[0] == "l1\n" and contenus[-1] == "l3\n"
    # numéros de lignes cohérents
    assert h0["lignes"][0]["old_no"] == 1
    assert h0["lignes"][0]["new_no"] == 1
    assert h0["lignes"][1]["new_no"] is None  # suppression : pas de n° nouveau
    assert h0["lignes"][2]["old_no"] is None  # ajout : pas de n° ancien


def test_contexte_zero():
    ancien = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
    nouveau = "a\nb\nc\nMODIF\ne\nf\ng\nh\ni\nj\n"
    res = build_diff(ancien, nouveau, contexte=0)
    assert res["stats"]["nb_hunks"] == 1
    assert res["hunks"][0]["old_count"] == 1 and res["hunks"][0]["new_count"] == 1


def test_stats():
    ancien, nouveau = _exemple_2_hunks()
    res = build_diff(ancien, nouveau, contexte=1)
    assert res["stats"]["ajouts"] == 3  # L2, l9a, l9b
    assert res["stats"]["suppressions"] == 2  # l2, l9
    assert res["stats"]["ancien_lignes"] == 12
    assert res["stats"]["nouveau_lignes"] == 13


def test_serialisation_dict():
    ancien, nouveau = _exemple_2_hunks()
    res = build_diff(ancien, nouveau, contexte=1)
    for d in res["hunks"]:
        h = Hunk.from_dict(d)
        assert apply_hunk(ancien, h) == apply_hunks(ancien, [d])
        assert h.as_dict()["old_start"] == d["old_start"]
        assert h.as_dict()["old_count"] == d["old_count"]
        assert h.as_dict()["new_start"] == d["new_start"]
        assert h.as_dict()["new_count"] == d["new_count"]


def test_source_hash_invalide():
    ancien = "a\nb\nc\n"
    res = build_diff(ancien, "a\nB\nc\n")
    try:
        apply_hunks("a\nAUTRE\nc\n", res["hunks"], source_hash=res["source_hash"])
    except ContenuStale:
        return
    raise AssertionError("ContenuStale attendue quand le fichier a changé")


def test_hunks_chevauchants():
    ancien = "a\nb\nc\nd\ne\nf\ng\nh\n"
    res = build_diff(ancien, "a\nB\nc\nd\ne\nF\ng\nh\n", contexte=1)
    assert res["stats"]["nb_hunks"] == 2
    # hunk 0 couvre [0, 3) ; on force un second hunk qui commence dedans.
    empiete = Hunk(id=1, old_a=2, old_b=5, new_a=2, new_b=5, lignes=[])
    try:
        apply_hunks(ancien, [res["hunks"][0], empiete])
    except HunksChevauchants:
        return
    raise AssertionError("HunksChevauchants attendue")


def test_coordonnees_hunk_creation():
    res = build_diff("", "x\ny\nz\n")
    assert res["stats"]["nb_hunks"] == 1
    h = res["hunks"][0]
    assert h["old_start"] == 0 and h["old_count"] == 0
    assert h["new_start"] == 1 and h["new_count"] == 3
    assert [l["type"] for l in h["lignes"]] == [TYPE_AJOUT] * 3


def test_coordonnees_hunk_suppression_totale():
    res = build_diff("x\ny\nz\n", "")
    assert res["stats"]["nb_hunks"] == 1
    h = res["hunks"][0]
    assert h["old_start"] == 1 and h["old_count"] == 3
    assert h["new_start"] == 0 and h["new_count"] == 0
    assert [l["type"] for l in h["lignes"]] == [TYPE_SUPPRESSION] * 3


# ────────────────────────────────────────────────────────────────────────────
# Runner autonome (sans pytest)
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