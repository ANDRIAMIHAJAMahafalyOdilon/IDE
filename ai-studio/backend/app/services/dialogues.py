"""Dialogues natifs Windows (tkinter, stdlib — aucune dépendance).

Le dialogue « Sélectionner un dossier » doit être appelé DEPUIS un thread de
worker sync (endpoint FastAPI `def`), pas depuis l'event loop : tkinter bloque
le thread appelant pendant que l'utilisateur navigue, et Starlette exécute les
endpoints synchrones dans son threadpool (anyio.to_thread) — les autres requêtes
HTTP restent donc servies pendant que la fenêtre est ouverte.

tkinter est importé paresseusement : si le Python hôte n'a pas de support Tk
(Python embarqué sans tkinter), l'app backend démarre quand même et l'endpoint
renvoie une erreur propre 503.
"""

from __future__ import annotations

import os
from pathlib import Path

_TITRE = "Sélectionner un dossier"


def choisir_dossier_natif() -> str:
    """Ouvre la vraie boîte de dialogue Windows « Sélectionner un dossier ».

    Bloque le temps du choix par l'utilisateur. Retourne le chemin absolu choisi,
    ou "" si l'utilisateur annule (ou fusionne la fenêtre). Lève RuntimeError si
    tkinter n'est pas disponible.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover — Python sans support Tk
        raise RuntimeError(
            "tkinter n'est pas disponible dans ce Python : "
            "impossible d'ouvrir la fenêtre système."
        ) from exc

    racine = tk.Tk()
    racine.withdraw()  # fenêtre mère invisible ; le dialogue natif reste modale
    try:
        # Sans topmost, la boîte Windows peut s'ouvrir DERRIÈRE le navigateur :
        # `-topmost` force le dialogue à passer au premier plan.
        racine.attributes("-topmost", True)
        racine.update_idletasks()
        chemin = filedialog.askdirectory(
            parent=racine,
            title=_TITRE,
            initialdir=_depart(),
            mustexist=True,
        )
    finally:
        try:
            racine.destroy()
        except Exception:
            pass
    if not (chemin or "").strip():
        return ""
    # tkinter renvoie des '/' sur Windows ; on normalise en chemin natif.
    return str(Path(chemin).resolve())


def _depart() -> str:
    """Dossier de départ du dialogue : Documents (sinon le profil utilisateur)."""
    profil = os.environ.get("USERPROFILE") or str(Path.home())
    docs = Path(profil) / "Documents"
    return str(docs if docs.is_dir() else Path(profil))