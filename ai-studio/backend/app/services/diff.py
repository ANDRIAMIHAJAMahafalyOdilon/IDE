"""diff.py — cœur technique d'AI Studio.

Convertit deux versions d'un fichier (annoté « ancien » / « nouveau ») en un
diff structuré, découpé en *hunks* indépendants, puis permet de recomposer un
fichier final à partir d'un sous-ensemble de hunks acceptés (validation
granulaire « façon Cursor » : accept/reject par bloc).

Contrats garantis par les tests :
  1.  build_diff(ancien, nouveau) + apply_hunks(ancien, TOUS les hunks) == nouveau
  2.  apply_hunks(ancien, AUCUN hunk) == ancien
  3.  Chaque hunk est autonome (coordonnées + lignes) : accept/reject par hunk
  4.  apply_hunks() vérifie que le fichier n'a pas changé entre-temps (source_hash)

Stratégie (stdlib uniquement) :
  - difflib.SequenceMatcher fournit les opcodes (equal / replace / delete / insert)
  - les blocs modifiés sont regroupés avec un contexte configurable (façon diff unifié)
  - la recomposition applique « ancien + hunks acceptés », sans dépendre du nouveau
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping

_CONTEXTE_PAR_DEFAUT = 3

# Types de lignes portées par un hunk (tokens ASCII, stables pour le frontend).
TYPE_CONTEXTE = "context"
TYPE_AJOUT = "add"
TYPE_SUPPRESSION = "del"


# ────────────────────────────────────────────────────────────────────────────
# Erreurs
# ────────────────────────────────────────────────────────────────────────────

class ErreurDiff(Exception):
    """Erreur métier du module diff."""


class HunksChevauchants(ErreurDiff):
    """Deux hunks d'un même diff ne doivent pas se chevaucher."""


class ContenuStale(ErreurDiff):
    """Le fichier a changé depuis la génération du diff (source_hash erroné)."""


# ────────────────────────────────────────────────────────────────────────────
# Modèle de données
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Hunk:
    """Un bloc de modification autonome du diff.

    Les bornes 0-based (old_a/old_b, new_a/new_b) servent à la recomposition ;
    les propriétés affichables (old_start/new_start/counts) suivent les
    conventions git du `diff unifié` pour le rendu Monaco.
    """

    id: int
    old_a: int  # 0-based : début des lignes anciennes concernées
    old_b: int  # 0-based : fin exclusive des lignes anciennes concernées
    new_a: int  # 0-based : début des lignes nouvelles concernées
    new_b: int  # 0-based : fin exclusive des lignes nouvelles concernées
    lignes: list[Mapping[str, Any]]  # {type, contenu, old_no, new_no}

    # ── Propriétés affichables (conventions diff unifié) ──
    @property
    def old_start(self) -> int:
        return self.old_a if self.old_a == self.old_b else self.old_a + 1

    @property
    def old_count(self) -> int:
        return self.old_b - self.old_a

    @property
    def new_start(self) -> int:
        return self.new_a if self.new_a == self.new_b else self.new_a + 1

    @property
    def new_count(self) -> int:
        return self.new_b - self.new_a

    @property
    def nouvelles_lignes(self) -> list[str]:
        """Les lignes qui subsistent du côté « nouveau » pour ce hunk."""
        return [l["contenu"] for l in self.lignes if l["type"] in (TYPE_CONTEXTE, TYPE_AJOUT)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "old_start": self.old_start,
            "old_end": self.old_start + self.old_count,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_end": self.new_start + self.new_count,
            "new_count": self.new_count,
            "lignes": self.lignes,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Hunk":
        lines = d.get("lignes") or []
        # Recalcul exact des bornes 0-based à partir des infos sérialisées.
        old_a = d.get("old_a")
        new_a = d.get("new_a")
        if old_a is None or new_a is None:
            old_start = d.get("old_start", 1)
            old_count = d.get("old_count", 0)
            new_start = d.get("new_start", 1)
            new_count = d.get("new_count", 0)
            old_a = old_start - 1 if old_count else old_start
            new_a = new_start - 1 if new_count else new_start
        return cls(
            id=int(d.get("id", 0)),
            old_a=old_a,
            old_b=old_a + d.get("old_count", 0),
            new_a=new_a,
            new_b=new_a + d.get("new_count", 0),
            lignes=list(lines),
        )


def _sha(contenu: str) -> str:
    return hashlib.sha1(contenu.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────────
# Construction du diff
# ────────────────────────────────────────────────────────────────────────────

def _groupes_avec_contexte(
    opcodes: list[tuple[str, int, int, int, int]],
    nb_ancien: int,
    nb_nouveau: int,
    contexte: int,
) -> Iterable[tuple[int, int, int, int]]:
    """Regroupe les opcodes de modification en régions autonomes.

    Rend (a1, a2, b1, b2) avec a=[ancien] et b=[nouveau], incluant `contexte`
    lignes de part et d'autre et ne fusionnant que les blocs séparés par moins
    de 2 * contexte lignes identiques (règle du diff unifié).
    """
    i = 0
    while i < len(opcodes):
        tag, a1, a2, b1, b2 = opcodes[i]
        if tag == "equal":
            i += 1
            continue

        debut_a, debut_b = a1, b1
        fin_a, fin_b = a2, b2
        i += 1

        while i < len(opcodes):
            tag2, a3, a4, b3, b4 = opcodes[i]
            if tag2 == "equal":
                if a4 - a3 >= 2 * contexte:
                    break  # assez de lignes identiques : on clôt le hunk
                fin_a, fin_b = a4, b4  # bloc trop court : absorbé dans le hunk
            else:
                fin_a, fin_b = a4, b4
            i += 1

        debut_a = max(0, debut_a - contexte)
        debut_b = max(0, debut_b - contexte)
        fin_a = min(nb_ancien, fin_a + contexte)
        fin_b = min(nb_nouveau, fin_b + contexte)
        yield debut_a, fin_a, debut_b, fin_b


def _produire_lignes(
    ancien: list[str], nouveau: list[str], a1: int, a2: int, b1: int, b2: int
) -> list[dict[str, Any]]:
    """Résout les différences à l'intérieur d'une région et produit les lignes
    typées (context / del / add) accompagnées de leurs numéros de ligne."""
    opcodes = difflib.SequenceMatcher(None, ancien[a1:a2], nouveau[b1:b2]).get_opcodes()
    lignes: list[dict[str, Any]] = []
    no_a, no_b = a1, b1

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i1, i2):
                lignes.append({
                    "type": TYPE_CONTEXTE,
                    "contenu": ancien[a1 + k],
                    "old_no": no_a + 1,
                    "new_no": no_b + 1,
                })
                no_a += 1
                no_b += 1
        elif tag == "replace":
            for k in range(i1, i2):
                lignes.append({"type": TYPE_SUPPRESSION, "contenu": ancien[a1 + k],
                               "old_no": no_a + 1, "new_no": None})
                no_a += 1
            for k in range(j1, j2):
                lignes.append({"type": TYPE_AJOUT, "contenu": nouveau[b1 + k],
                               "old_no": None, "new_no": no_b + 1})
                no_b += 1
        elif tag == "delete":
            for k in range(i1, i2):
                lignes.append({"type": TYPE_SUPPRESSION, "contenu": ancien[a1 + k],
                               "old_no": no_a + 1, "new_no": None})
                no_a += 1
        elif tag == "insert":
            for k in range(j1, j2):
                lignes.append({"type": TYPE_AJOUT, "contenu": nouveau[b1 + k],
                               "old_no": None, "new_no": no_b + 1})
                no_b += 1

    return lignes


def build_diff(ancien: str, nouveau: str, contexte: int = _CONTEXTE_PAR_DEFAUT) -> dict[str, Any]:
    """Calcule le diff structuré entre deux contenus de fichier.

    Retourne : {modifie, source_hash, hunks, stats}
    """
    if contexte < 0:
        raise ValueError("contexte doit être >= 0")

    lignes_a = ancien.splitlines(keepends=True)
    lignes_b = nouveau.splitlines(keepends=True)

    opcodes = difflib.SequenceMatcher(None, lignes_a, lignes_b).get_opcodes()

    hunks: list[Hunk] = []
    for idx, (a1, a2, b1, b2) in enumerate(
        _groupes_avec_contexte(opcodes, len(lignes_a), len(lignes_b), contexte)
    ):
        hunks.append(Hunk(
            id=idx,
            old_a=a1,
            old_b=a2,
            new_a=b1,
            new_b=b2,
            lignes=_produire_lignes(lignes_a, lignes_b, a1, a2, b1, b2),
        ))

    ajouts = sum(1 for h in hunks for l in h.lignes if l["type"] == TYPE_AJOUT)
    suppressions = sum(1 for h in hunks for l in h.lignes if l["type"] == TYPE_SUPPRESSION)

    return {
        "modifie": bool(hunks),
        "source_hash": _sha(ancien),
        "hunks": [h.as_dict() for h in hunks],
        "stats": {
            "ajouts": ajouts,
            "suppressions": suppressions,
            "nb_hunks": len(hunks),
            "ancien_lignes": len(lignes_a),
            "nouveau_lignes": len(lignes_b),
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# Recomposition (validation granulaire)
# ────────────────────────────────────────────────────────────────────────────

def _normaliser_hunks(hunks: Iterable[Any]) -> list[Hunk]:
    """Accepte des instances Hunk ou des dicts sérialisés (as_dict -> from_dict)."""
    res: list[Hunk] = []
    for h in hunks:
        if isinstance(h, Hunk):
            res.append(h)
        elif isinstance(h, Mapping):
            res.append(Hunk.from_dict(dict(h)))
        else:
            raise TypeError(f"hunk invalide : {type(h)!r}")
    return res


def apply_hunks(ancien: str, hunks: Iterable[Any], source_hash: str | None = None) -> str:
    """Recompose un fichier final en appliquant uniquement les hunks acceptés.

    Remarque : les hunks ne se chevauchent jamais (ils proviennent d'un seul
    build_diff) ; l'ordre d'application est donc l'ordre « ancien ».
    """
    if source_hash is not None and _sha(ancien) != source_hash:
        raise ContenuStale(
            "Le fichier a changé depuis la génération du diff. Régénère le "
            "diff avant d'appliquer les modifications."
        )

    lignes = ancien.splitlines(keepends=True)
    accepts = sorted(_normaliser_hunks(hunks), key=lambda h: h.old_a)

    # Détection défensive de chevauchement / ordre incohérent.
    precedent = 0
    for h in accepts:
        if h.old_a < precedent or h.old_b < 0 or h.old_b < h.old_a or h.old_b > len(lignes):
            raise HunksChevauchants(
                f"hunk {h.id}: coordonnées invalides dans l'ancien fichier "
                f"({h.old_a}..{h.old_b}, {len(lignes)} lignes)."
            )
        precedent = h.old_b

    morceaux: list[str] = []
    curseur = 0
    for h in accepts:
        morceaux.append("".join(lignes[curseur:h.old_a]))
        morceaux.append("".join(h.nouvelles_lignes))
        curseur = h.old_b
    morceaux.append("".join(lignes[curseur:]))

    return "".join(morceaux)


def apply_hunk(ancien: str, hunk: Any, source_hash: str | None = None) -> str:
    """Applique un seul hunk (raccourci pour le accept/reject par bloc)."""
    return apply_hunks(ancien, [hunk], source_hash=source_hash)