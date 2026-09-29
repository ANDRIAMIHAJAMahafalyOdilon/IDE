"""Endpoints des projets (import, arborescence, fichiers, recherche).

Contrat : docs/cahier-des-charges-api.md. Tous les chemins sont relatifs au
projet et protégés contre le "path traversal" (CheminHorsProjet -> 422).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..services import projets as ps
from ..services.workspace import CheminHorsProjet, racine_projet

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _http(exc: Exception, rel: str | None = None) -> HTTPException:
    if isinstance(exc, (CheminHorsProjet, IsADirectoryError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("")
def lister():
    """Liste les projets existants : [{id, nom, nb_fichiers}]."""
    return {"projets": ps.lister_projets()}


@router.post("")
def creer_vide(payload: dict[str, Any] | None = None):
    """Crée (ou remplace) un projet vide à partir de son nom."""
    nom = (payload or {}).get("nom", "")
    try:
        racine_projet(nom)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": nom, "nom": nom, "nb_fichiers": 0}


@router.post("/import")
async def importer(fichier: UploadFile, nom: str | None = Form(default=None)):
    """Importe une archive zip/tar/tar.gz ; remplace le projet du même nom."""
    binaire = await fichier.read()
    try:
        return ps.importer_archive(binaire, nom)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/browse")
def parcourir(chemin: str = ""):
    """Explorateur serveur de dossiers (bouton « Parcourir… »).

    `chemin` vide -> points de départ usuels (session, Documents, Desktop…) ;
    sinon liste les sous-dossiers de *chemin* (tri alpha). Chaque entrée listée
    (sous-dossier comme remontée parent) a déjà passé `valider_chemin_local` :
    l'explorateur ne peut jamais montrer un chemin que l'ouverture refuserait.

    Ne référence jamais : aucun enregistrement, aucune écriture.
    """
    try:
        return ps.parcourir_dossier(chemin)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/preview-local")
def apercu_local(payload: dict[str, Any]):
    """Aperçu (sans enregistrement) d'un dossier local : validation + échantillon.

    Ne touche à rien : même validation de sécurité que l'ouverture réelle.
    """
    try:
        return ps.apercu_dossier_local((payload or {}).get("chemin", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/open-local")
def ouvrir_local(payload: dict[str, Any]):
    """Ouvre un dossier local en mode DIRECT (aucune copie) : référence le
    chemin réel du disque via le registre. Refuse les chemins sensibles.

    Ensuite, tous les endpoints existants (/tree, /file, /search, /etat,
    /agent/apply-changes en accept/reject) agissent sur le vrai disque.
    """
    try:
        return ps.ouvrir_dossier_local((payload or {}).get("chemin", ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{projet}/export")
def exporter(projet: str, background: BackgroundTasks):
    """Archive zip du projet, prête à être téléchargée.

    Symétrique de `POST /import` : c'est ce qui referme la boucle quand
    l'agent a modifié des fichiers dans le cloud. L'archive part dans un
    dossier racine homonyme au projet, donc elle se réimporte telle quelle.
    """
    try:
        chemin, nom_fichier = ps.exporter_archive(projet)
    except CheminHorsProjet as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background.add_task(ps.supprimer_temporaire, chemin)
    return FileResponse(
        chemin, media_type="application/zip", filename=nom_fichier
    )


@router.get("/{projet}/tree")
def tree(projet: str):
    """Arborescence imbriquée (dossiers puis fichiers, tri alpha)."""
    try:
        return {"projet": projet, "racine": ps.arborescence(projet)}
    except (ValueError, CheminHorsProjet) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{projet}/file")
def lire(projet: str, chemin: str):
    """Contenu texte du fichier *chemin*. 404 si absent, 422 si binaire/lourd."""
    try:
        return ps.lire_fichier(projet, chemin)
    except (CheminHorsProjet, IsADirectoryError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "binaire", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{projet}/file")
def ecrire(projet: str, payload: dict[str, Any]):
    """Sauvegarde éditeur : écrit *contenu* à *chemin* (dossiers parents créés)."""
    chemin = payload.get("chemin", "")
    contenu = payload.get("contenu", "")
    try:
        return ps.ecrire_fichier(projet, chemin, contenu)
    except (CheminHorsProjet, IsADirectoryError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{projet}/search")
def rechercher(projet: str, q: str = ""):
    """Grep simple (sous-chaîne, insensible à la casse) avec contexte ±1 ligne."""
    try:
        return ps.rechercher(projet, q)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CheminHorsProjet as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{projet}/etat")
def etat(projet: str, chemins: list[str] | None = None):
    """Empreintes SHA-1 des fichiers (poll du frontend pendant les tâches agent).

    `chemins` est répétable (GET /etat?chemins=a.py&chemins=b.py) ; absent =
    tout le projet.

    Un projet inconnu est un 404, pas une erreur serveur : le frontend poll
    cet endpoint et interpretait le 500 comme une panne du backend. Note :
    `ps.etat_fichiers` ignore volontairement les chemins hors projet, donc
    seule l'absence du projet remonte ici.
    """
    try:
        return ps.etat_fichiers(projet, chemins)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc