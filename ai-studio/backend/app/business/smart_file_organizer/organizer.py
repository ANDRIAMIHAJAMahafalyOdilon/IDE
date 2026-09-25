"""Logique de déplacement vers les dossiers de catégories (port adapté).

Ajouts de robustesse par rapport à l'ancienne app :
- garde-fou : refuse l'espace projets et les dépôts git (jamais de réorg.
  silencieuse d'un projet) ;
- collisions : le nom de destination est dédupliqué (« nom (2).ext ») au lieu
  d'écraser ;
- `analyser()` fait une prévisualisation NON destructive (dry-run).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import config, db
from .scanner import categorize_file, list_files


class OrganizerErreur(ValueError):
    """Rejet d'une opération d'organisation (dossier interdit, etc.)."""


def valider_racine(repertoire: str | Path) -> Path:
    """Respecte *repertoire*, vérifie qu'on a le droit de l'organiser.

    Le refus « interdit » prime sur l'existence : un chemin interdit reste
    refusé même s'il n'existe pas encore.
    """
    racine = Path(repertoire).expanduser().resolve()
    for interdit in config.REPERTOIRES_INTERDITS:
        if interdit is None:
            continue
        interdit = Path(interdit).resolve()
        if racine == interdit or interdit in racine.parents:
            raise OrganizerErreur(
                f"dossier interdit par la politique d'organisation : {racine}"
            )
    if not racine.is_dir():
        raise FileNotFoundError(str(racine))
    if (racine / ".git").exists():
        raise OrganizerErreur(f"dépôt git refusé (risque de désorganisation) : {racine}")
    return racine


def _nom_unique(dossier: Path, nom: str) -> str:
    """Déduplique *nom* si la destination existe déjà."""
    if not (dossier / nom).exists():
        return nom
    p = Path(nom)
    base, ext = p.stem, p.suffix
    i = 2
    while (dossier / f"{base} ({i}){ext}").exists():
        i += 1
    return f"{base} ({i}){ext}"


def _sortie(root_dir: Path) -> Path:
    """Dossier des catégories = frère du dossier source."""
    return root_dir.parent / config.BASE_OUTPUT_DIR


def analyser(repertoire: str | Path) -> dict:
    """Prévisualisation : répartition par catégorie, SANS déplacer."""
    racine = valider_racine(repertoire)
    par_categorie: dict[str, list[dict]] = {}
    taille_totale = 0
    for f in list_files(racine):
        categorie, ext = categorize_file(f)
        taille = f.stat().st_size
        taille_totale += taille
        par_categorie.setdefault(categorie, []).append({
            "chemin": str(f),
            "relative": str(f.relative_to(racine)),
            "taille": taille,
            "extension": ext,
        })
    return {
        "repertoire": str(racine),
        "nb_fichiers": sum(len(v) for v in par_categorie.values()),
        "taille_totale": taille_totale,
        "sortie": str(_sortie(racine)),
        "par_categorie": [
            {"categorie": cat, "fichiers": fichiers}
            for cat, fichiers in sorted(par_categorie.items())
        ],
    }


def organiser(repertoire: str | Path) -> dict:
    """Déplace chaque fichier dans sa catégorie et journalise en base."""
    racine = valider_racine(repertoire)
    sortie = _sortie(racine)
    db.init_db()

    deplacements: dict[str, int] = {}
    taille_totale = 0
    for f in list_files(racine):
        categorie, _ = categorize_file(f)
        cible = sortie / categorie
        cible.mkdir(parents=True, exist_ok=True)
        dst = cible / _nom_unique(cible, f.name)
        taille_totale += f.stat().st_size
        shutil.move(str(f), str(dst))
        db.log_move(f, dst)
        deplacements[categorie] = deplacements.get(categorie, 0) + 1

    total = sum(deplacements.values())
    if total:
        db.update_stats(total, taille_totale)
    return {
        "repertoire": str(racine),
        "sortie": str(sortie),
        "deplaces": total,
        "taille_totale": taille_totale,
        "par_categorie": deplacements,
    }