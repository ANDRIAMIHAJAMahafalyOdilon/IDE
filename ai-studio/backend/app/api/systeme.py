"""Endpoints système de la machine locale (hors périmètre « projets »).

Usage : aider le frontend à ouvrir des ressources du disque réel via des
boîtes de dialogue Windows natives.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException

from ..services.dialogues import choisir_dossier_natif
from ..services.registre import valider_chemin_local

router = APIRouter(prefix="/api/system", tags=["system"])


def _tkinter_disponible() -> bool:
    """Vrai seulement si cet interpréteur peut ouvrir une vraie fenêtre système.

    Le disque local n'a de sens que sur la machine de l'utilisateur : une
    instance cloud (Linux, ou Python sans Tk) répond 503 sur ces endpoints.
    Le frontend s'en sert pour ne pas afficher de boutons morts.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


@router.get("/capacites")
def capacites():
    """Lesquelles des fonctions dépendent de la machine locale de l'utilisateur.

    `dossier_local` couvre tout ce qui lit le disque du poste : fenêtre
    système, aperçu et ouverture d'un projet hors des données du serveur.
    Le reste (import, export, agent, chat) fonctionne partout.
    """
    return {
        "dossier_local": _tkinter_disponible(),
        "plateforme": sys.platform,
    }


@router.post("/choisir-dossier")
def choisir_dossier():
    """Ouvre la VRAIE fenêtre Windows « Sélectionner un dossier » (tkinter).

    Bloque le temps que l'utilisateur choisisse (ou annule) puis renvoie le
    chemin choisi (ou une chaîne vide si annulé). Le chemin est validé par
    `valider_chemin_local` — le même garde-fou que partout ailleurs (422 si
    refusé : racine de lecteur, dossier système, AppData…).

    Batching : la demande HTTP reste ouverte pendant toute la navigation ;
    le handler est `def` (synchrone) -> Starlette l'exécute dans son threadpool,
    donc le serveur continue de répondre aux autres requêtes.
    """
    try:
        brut = choisir_dossier_natif()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not brut:
        return {"chemin": "", "nom": ""}
    try:
        chemin, nom = valider_chemin_local(brut)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"chemin": str(chemin), "nom": nom}