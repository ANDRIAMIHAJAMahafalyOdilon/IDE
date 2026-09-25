"""Endpoints du Smart File Organizer (port de la fonctionnalité métier).

Dry-run obligatoire avant l'exécution : /api/organizer/analyser affiche la
répartition SANS déplacer ; /api/organizer/organiser ne fait ensuite que ce qui
a été prévu. Refus catégoriques : espace projets et dépôts git.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..business.smart_file_organizer import config as org_config
from ..business.smart_file_organizer import db as org_db
from ..business.smart_file_organizer.organizer import (
    OrganizerErreur,
    analyser,
    organiser,
)

router = APIRouter(prefix="/api/organizer", tags=["organizer"])


def _repertoire(payload: dict[str, Any] | None) -> str:
    rep = (payload or {}).get("repertoire", "")
    if not rep or not isinstance(rep, str):
        raise HTTPException(status_code=422, detail="repertoire requis")
    return rep


@router.get("/categories")
def categories():
    """Mapping extension -> catégorie (pour affichage / éditeur de règles)."""
    return {"categories": org_config.EXTENSION_MAP}


@router.post("/analyser")
def apercu(payload: dict[str, Any] | None = None):
    """Prévisualisation non destructive : répartition par catégorie."""
    try:
        return analyser(_repertoire(payload))
    except OrganizerErreur as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/organiser")
def executer(payload: dict[str, Any] | None = None):
    """Déplace effectivement les fichiers puis journalise en base."""
    try:
        return organiser(_repertoire(payload))
    except OrganizerErreur as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/historique")
def historique(limit: int = 20):
    """Derniers déplacements enregistrés."""
    org_db.init_db()
    return {"deplacements": org_db.fetch_history(min(max(limit, 1), 200))}


@router.get("/stats")
def stats():
    """Compteurs globaux (fichiers déplacés, espace total)."""
    org_db.init_db()
    total_files, total_size = org_db.fetch_stats()
    return {"total_files": total_files, "total_size": total_size}