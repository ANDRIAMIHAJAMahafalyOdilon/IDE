"""Accès disque sécurisé aux projets importés (lecture / écriture / liste).

Toutes les opérations se font par chemin RELATIF au dossier racine du projet,
avec une vérification anti "path traversal" : impossible de sortir du projet.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..config import (
    ARBORESCENCE_CACHE_SECONDES,
    FICHIER_CONTEXTE_MAX_CAR,
    FICHIER_CONTEXTE_MAX_LIGNES,
    FICHIERS_CONTEXTE_MAX,
    PROJETS_DIR,
)
from . import registre
from .filtres import filtres_pour


class CheminHorsProjet(ValueError):
    """Levée quand un chemin relatif tente de sortir du dossier racine."""


class PatchInvalide(ValueError):
    """Levée quand une opération de patch ne s'applique pas au fichier visé.

    Distincte de `CheminHorsProjet` : ici le chemin est bon, c'est le contenu
    qui ne correspond plus — le fichier a changé entre-temps, ou l'agent a
    inventé un numéro de ligne.
    """


def appliquer_patch(contenu: str, operations: list[dict]) -> str:
    """Applique des opérations de patch à *contenu* et renvoie le texte obtenu.

    Une opération est `{"ligne": N, "suppression": k, "ajout": [...]}`, où `N`
    est un numéro de ligne 1-based du fichier ACTUEL, `k` le nombre de lignes
    consecutivees à remplacer (1 par défaut) et `ajout` les nouvelles lignes à
    mettre à leur place.

    Pourquoi des opérations plutôt qu'un fichier complet : demander à un modèle
    de restituer un fichier entier est intenable dès que le fichier dépasse le
    contexte envoyé — il abandonne, ou renvoie un fichier amputé. Ici le modèle
    n'écrit que ce qui change, et le backend applique sur le contenu réel lu sur
    le disque au moment de la proposition.

    Les opérations sont appliquées de la FIN vers le DÉBUT : les numéros de
    ligne restent valides au fur et à mesure, sans recalcul d'offset. Deux
    opérations qui se chevauchent sont rejetées plutôt que fusionnées en silence,
    car un chevauchement signale que le modèle raisonne sur un fichier différent
    de celui qu'il a reçu.
    """
    if not operations:
        raise PatchInvalide("aucune opération de patch.")

    lignes = contenu.split("\n")
    taille = len(lignes)

    normalisees: list[tuple[int, int, list[str]]] = []
    for op in operations:
        if not isinstance(op, dict):
            raise PatchInvalide(f"opération non objet : {op!r}")
        try:
            depart = int(op["ligne"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PatchInvalide(
                f"opération sans numéro de ligne exploitable : {op!r}"
            ) from exc
        supp = op.get("suppression", 1)
        try:
            supp = int(supp)
        except (TypeError, ValueError) as exc:
            raise PatchInvalide(f"« suppression » illisible : {supp!r}") from exc
        if depart < 1:
            raise PatchInvalide(f"numéro de ligne {depart} : la numérotation démarre à 1.")
        if supp < 1:
            raise PatchInvalide(f"« suppression » doit valoir au moins 1, reçu {supp}.")
        if depart + supp - 1 > taille:
            raise PatchInvalide(
                f"lignes {depart}-{depart + supp - 1} hors du fichier, "
                f"qui n'en compte que {taille}."
            )
        ajout = op.get("ajout", [])
        if isinstance(ajout, str):
            ajout = [ajout]
        if not isinstance(ajout, list) or not all(isinstance(l, str) for l in ajout):
            raise PatchInvalide(f"« ajout » doit être une liste de lignes : {ajout!r}")
        normalisees.append((depart, supp, list(ajout)))

    normalisees.sort(key=lambda o: o[0], reverse=True)
    for (d1, s1, _), (d2, s2, _) in zip(normalisees, normalisees[1:]):
        # Trié par `ligne` décroissante, `normalisees[i+1]` est celle d'après.
        # La première couvre [d1, d1+s1-1] et la suivante [d2, d2+s2-1] avec
        # d1 >= d2 : elles se recouvrent si la seconde va jusqu'à d1.
        if d2 + s2 - 1 >= d1:
            raise PatchInvalide(
                f"opérations qui se chevauchent : lignes {d1}-{d1 + s1 - 1} "
                f"et {d2}-{d2 + s2 - 1}."
            )

    for depart, supp, ajout in normalisees:
        lignes[depart - 1 : depart - 1 + supp] = ajout
    return "\n".join(lignes)


_ARBORESCENCES: dict[tuple[str, int], tuple[float, str]] = {}


def _racine_importee(nom: str, creer: bool) -> Path:
    """Dossier du projet IMPORTÉ (archive) : PROJETS_DIR / nom."""
    if not nom or nom in (".", "..") or "/" in nom or "\\" in nom:
        raise ValueError(f"nom de projet invalide : {nom!r}")
    racine = PROJETS_DIR / nom
    if creer:
        racine.mkdir(parents=True, exist_ok=True)
        return racine
    if not racine.is_dir():
        raise FileNotFoundError(nom)
    return racine


def racine_projet(nom: str) -> Path:
    """Racine du projet *nom* : dossier direct (registre) sinon importé (créé)."""
    directe = registre.racine_directe(nom)
    if directe is not None:
        return directe
    return _racine_importee(nom, creer=True)


def projet_existant(nom: str) -> Path:
    """Valide *nom* et retourne sa racine sans la créer (404 si absent)."""
    directe = registre.racine_directe(nom)
    if directe is not None:
        return directe
    return _racine_importee(nom, creer=False)


def chemin_securise(racine: Path, rel: str) -> Path:
    """Résout *rel* sous *racine* et refuse toute sortie du projet."""
    base = racine.resolve()
    cible = (base / rel).resolve()
    if cible != base and base not in cible.parents:
        raise CheminHorsProjet(f"chemin hors du projet : {rel}")
    return cible


def lire_fichier(chemin: Path) -> str:
    """Lit un fichier en UTF-8 (tolérant aux octets inconnus)."""
    return chemin.read_text(encoding="utf-8", errors="replace")


def lire_fichier_ou(chemin: Path, defaut: str = "") -> str:
    """Lit un fichier ; retourne *defaut* s'il n'existe pas (cas création)."""
    if not chemin.exists():
        return defaut
    return lire_fichier(chemin)


def ecrire_fichier(chemin: Path, contenu: str) -> None:
    """Écrit un fichier (crée les dossiers parents si besoin)."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(contenu, encoding="utf-8")


def lister_arborescence(racine: Path) -> list[str]:
    """Liste (triée) des fichiers relatifs du projet, horodatés par importance.

    Applique les filtres (dossiers lourds + .gitignore du projet : jamais des
    dizaines de milliers de fichiers inutiles dans le contexte de l'agent).
    """
    if not racine.exists():
        return []
    flt = filtres_pour(racine)
    return sorted(
        str(p.relative_to(racine))
        for p in racine.rglob("*")
        if p.is_file() and not flt.ignore(p.relative_to(racine).as_posix())
    )


def arborescence_texte(racine: Path, limite: int = 200) -> str:
    """Représentation compacte du projet pour le contexte de l'agent."""
    cle = (str(racine.resolve()), limite)
    maintenant = time.monotonic()
    cache = _ARBORESCENCES.get(cle)
    if cache and maintenant - cache[0] < ARBORESCENCE_CACHE_SECONDES:
        return cache[1]
    fichiers = lister_arborescence(racine)
    if not fichiers:
        texte = "(projet vide)"
    else:
        if len(fichiers) > limite:
            affiches = fichiers[:limite]
            extra = f"\n… +{len(fichiers) - limite} fichiers"
        else:
            affiches, extra = fichiers, ""
        texte = "\n".join(affiches) + extra
    _ARBORESCENCES[cle] = (maintenant, texte)
    return texte


def invalider_arborescence(racine: Path | None = None) -> None:
    """Invalide le cache après une création ou suppression de fichier."""
    if racine is None:
        _ARBORESCENCES.clear()
        return
    prefixe = str(racine.resolve())
    for cle in [cle for cle in _ARBORESCENCES if cle[0] == prefixe]:
        _ARBORESCENCES.pop(cle, None)


def bloc_fichiers_contexte(
    chemins_contexte: list[str],
    racine: Path,
    limite: int = FICHIERS_CONTEXTE_MAX,
    limite_car: int = FICHIER_CONTEXTE_MAX_CAR,
    limite_lignes: int = FICHIER_CONTEXTE_MAX_LIGNES,
) -> str:
    """Lit et limite les fichiers de contexte (lu côté backend, jamais côté
    frontend) - max `limite_car` caractères et `limite_lignes` lignes chacun,
    `limite` fichiers.

    Chaque ligne est préfixée de son numéro (`   12 | code`) : c'est ce qui
    permet à l'agent de patcher le fichier en citant des lignes précises plutôt
    qu'en réécrivant le fichier entier. Le préfixe est à six caractères de large,
    donc aligné jusqu'à 999999 lignes.

    `limite_car` et `limite_lignes` sont paramétrables parce que le mode Edit a
    besoin d'un budget bien plus large que le Chat : il doit voir la ligne à
    modifier, sinon il ne peut pas la situer.
    """
    morceaux: list[str] = []
    for rel in [c for c in chemins_contexte if c][:limite]:
        try:
            chemin = chemin_securise(racine, rel)
        except CheminHorsProjet:
            continue
        contenu = lire_fichier_ou(chemin, "")
        if not contenu:
            continue
        numeroes = "\n".join(
            f"{i:>6} | {ligne}"
            for i, ligne in enumerate(
                contenu[:limite_car].split("\n")[:limite_lignes], 1
            )
        )
        morceaux.append(f"===FICHIER {rel}===\n{numeroes}\n===FIN FICHIER===")
    return "\n\n".join(morceaux)
