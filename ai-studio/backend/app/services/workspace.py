"""Accès disque sécurisé aux projets importés (lecture / écriture / liste).

Toutes les opérations se font par chemin RELATIF au dossier racine du projet,
avec une vérification anti "path traversal" : impossible de sortir du projet.
"""

from __future__ import annotations

from pathlib import Path

from ..config import FICHIER_CONTEXTE_MAX_CAR, FICHIERS_CONTEXTE_MAX, PROJETS_DIR
from . import registre
from .filtres import filtres_pour


class CheminHorsProjet(ValueError):
    """Levée quand un chemin relatif tente de sortir du dossier racine."""


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
    fichiers = lister_arborescence(racine)
    if not fichiers:
        return "(projet vide)"
    if len(fichiers) > limite:
        affiches = fichiers[:limite]
        extra = f"\n… +{len(fichiers) - limite} fichiers"
    else:
        affiches, extra = fichiers, ""
    return "\n".join(affiches) + extra


def bloc_fichiers_contexte(
    chemins_contexte: list[str],
    racine: Path,
    limite: int = FICHIERS_CONTEXTE_MAX,
) -> str:
    """Lit et limite les fichiers de contexte (lu côté backend, jamais côté
    frontend) — max FICHIER_CONTEXTE_MAX_CAR chacun, `limite` fichiers."""
    morceaux: list[str] = []
    for rel in [c for c in chemins_contexte if c][:limite]:
        try:
            chemin = chemin_securise(racine, rel)
        except CheminHorsProjet:
            continue
        contenu = lire_fichier_ou(chemin, "")
        if not contenu:
            continue
        contenu = contenu[:FICHIER_CONTEXTE_MAX_CAR]
        morceaux.append(f"===FICHIER {rel}===\n{contenu}\n===FIN FICHIER===")
    return "\n\n".join(morceaux)