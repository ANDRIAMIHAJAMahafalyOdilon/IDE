"""Application des modifications validées — cœur de /api/agent/apply-changes.

Décision d'architecture (option A) : `build_diff()` est systématiquement
appelé CÔTÉ BACKEND, au moment où l'agent produit sa proposition brute,
AVANT tout envoi au frontend (SSE). Le frontend ne voit jamais de contenu
brut : il reçoit des hunks, renvoie les hunks acceptés, et cette couche les
applique sur le fichier réel, protégée par le `source_hash` (péremption si le
fichier a bougé entre la proposition et l'application).

Ce module ne dépend que de workspace + diff (pas de FastAPI) : il est testable
en isolation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import workspace
from .diff import ContenuStale, HunksChevauchants, apply_hunks


@dataclass
class ResultatFichier:
    """Résultat d'application pour un fichier (sans dépendance Pydantic)."""

    fichier: str
    statut: str  # "ok" | "erreur" | "pas_modifie"
    message: str | None = None
    nouveau_sha: str | None = None


def _sha(contenu: str) -> str:
    return hashlib.sha1(contenu.encode("utf-8")).hexdigest()


def appliquer_changements(
    racine: Path,
    modifications: Sequence[Mapping[str, Any]],
) -> list[ResultatFichier]:
    """Applique chaque changement dict {fichier, action, source_hash, acceptes}."""
    return [_appliquer_un(racine, m) for m in modifications]


def _appliquer_un(racine: Path, m: Mapping[str, Any]) -> ResultatFichier:
    rel = str(m.get("fichier") or "")
    action = m.get("action") or "write"

    # Fatal : tentative de sortie du projet -> remonte jusqu'à l'API (422).
    chemin = workspace.chemin_securise(racine, rel)

    if action == "delete":
        if chemin.exists():
            chemin.unlink()
        return ResultatFichier(rel, "ok")

    try:
        acceptes = list(m.get("acceptes") or [])
        if not acceptes:
            return ResultatFichier(rel, "pas_modifie")

        source_hash = m.get("source_hash")
        ancien = workspace.lire_fichier_ou(chemin, "")
        nouveau = apply_hunks(ancien, acceptes, source_hash=source_hash)
        workspace.ecrire_fichier(chemin, nouveau)
        return ResultatFichier(rel, "ok", nouveau_sha=_sha(nouveau))
    except ContenuStale as exc:
        return ResultatFichier(
            rel,
            "erreur",
            f"fichier périmé depuis la proposition (il a changé entre-temps) : {exc}",
        )
    except HunksChevauchants as exc:
        return ResultatFichier(rel, "erreur", f"hunks invalides : {exc}")
    except OSError as exc:
        return ResultatFichier(rel, "erreur", f"erreur disque : {exc}")