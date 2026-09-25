"""Registre des projets ouverts en mode « dossier direct ».

Un projet « dossier direct » référence un dossier réel du disque local (ouverture
VS Code-like, pas de copie : tout /tree, /file, /search, /apply agit en lecture /
écriture sur le vrai disque). Ce registre mappe un id logique -> chemin absolu
et est persisté en JSON dans data/registre.json.

Les projets « importés » (archive zip/tar) restent, eux, sous PROJETS_DIR — et ne
passent jamais par ce registre.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from ..config import DATA_DIR, PROJETS_DIR, PROJECT_ROOT

# Répertoires que le registre ne gère pas (protection ultime, indépendante du
# contenu du .gitignore) : impossible d'ouvrir un dossier système en mode direct.
_AUTREFOIS = (DATA_DIR, PROJETS_DIR, PROJECT_ROOT)

# Variables d'environnement dont la VALEUR est un dossier système à refuser ainsi
# que tous ses sous-dossiers.
_SYS_VAR = (
    "SystemRoot",  # C:\Windows
    "WINDIR",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramData",
)

# Fichier de persistance. Surchargeable (REGISTRE_FICHIER) pour les tests/smoke
# live sans toucher au registre réel data/registre.json.
_FICHIER = Path(os.getenv("REGISTRE_FICHIER", str(DATA_DIR / "registre.json")))
_VERROU = threading.Lock()


def _charger() -> dict[str, dict]:
    try:
        raw = json.loads(_FICHIER.read_text(encoding="utf-8"))
        projets = raw.get("projets", {}) if isinstance(raw, dict) else {}
    except FileNotFoundError:
        projets = {}
    except (ValueError, OSError):
        projets = {}
    return {str(k): v for k, v in projets.items() if isinstance(v, dict)}


def _sauver(projets: dict[str, dict]) -> None:
    _FICHIER.parent.mkdir(parents=True, exist_ok=True)
    _FICHIER.write_text(
        json.dumps({"projets": projets}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─────────────────────────────── Validation ────────────────────────────────

def _refus(nom: str, sous: Path | None = None) -> bool:
    """True si *sous* == *nom* ou se trouve DANS *nom*."""
    if not sous:
        return False
    return sous == nom or sous.is_relative_to(nom)


def _racines_invalides() -> list[Path]:
    """Racines système / sensibles jamais exploitables (refusés = eux + enfants).

    Ne contient PAS les racines de lecteur (C:\\, D:\\…) : le dossier racine est
    déjà refusé par le contrôle d'anchor dans `valider_chemin_local`. Les inclure
    ici refuserait **tout** chemin du disque (p.is_relative_to("C:\\") == True).
    """
    racines: list[Path] = []
    for var in _SYS_VAR:
        val = os.environ.get(var)
        if val:
            racines.append(Path(val).resolve())
    profil = os.environ.get("USERPROFILE")
    if profil:
        appdata = (Path(profil).resolve() / "AppData").resolve()
        racines.append(appdata)
    return racines


def _chemins_refuses_meta() -> list[Path]:
    return [p.resolve() for p in _AUTREFOIS]


def valider_chemin_local(texte: str) -> tuple[Path, str]:
    """Valide un chemin de dossier local et retourne (path résolu, nom proposé).

    Lève ValueError avec un message explicite pour chaque cause de refus.
    Toujours `.resolve()` (résout ../, liens symboliques) AVANT les contrôles.
    """
    brut = (texte or "").strip().strip("\"'\u00ab\u00bb").rstrip("\\/")
    if not brut:
        raise ValueError("Chemin vide.")

    p = Path(brut)
    if not p.is_absolute():
        raise ValueError(
            f"Chemin non absolu : {brut!r}. Colle un chemin complet (ex. C:\\Users\\…)."
        )
    p = p.resolve()

    if not p.exists():
        raise ValueError(f"Ce chemin n'existe pas : {p}")
    if not p.is_dir():
        raise ValueError(f"Ce n'est pas un dossier : {p}")

    # Racine d'un lecteur (C:\, D:\…) : refuse le dossier racine même.
    if p == Path(p.anchor).resolve():
        raise ValueError("Refusé : ouverture d'une racine de lecteur. Choisis un sous-dossier.")

    for racine in _racines_invalides() + _chemins_refuses_meta():
        if _refus(racine, p) or _refus(p, racine):
            raise ValueError(f"Chemin sensible refusé : {p}")

    nom = p.name.strip() or "dossier"
    return p, nom


def _slug(nom: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", nom).strip(".-_") or "dossier"
    return slug


def id_disponible(nom: str) -> str:
    """ID unique : slug du dossier, suffixé si collision avec un projet existant."""
    base = _slug(nom)
    pris = set(_charger().keys()) | {
        d.name for d in PROJETS_DIR.iterdir() if d.is_dir()
    }
    if base not in pris:
        return base
    i = 2
    while f"{base}-{i}" in pris:
        i += 1
    return f"{base}-{i}"


# ─────────────────────────────── API du registre ───────────────────────────

def _chemin_entree(entree: dict) -> Path | None:
    brut = entree.get("chemin", "")
    if not brut:
        return None
    try:
        return Path(brut).resolve()
    except (OSError, ValueError):
        return None


def trouver_par_chemin(texte: str) -> dict | None:
    """Entrée existante du registre pointant vers CE dossier, sinon None.

    Compare les chemins RÉSOLUS : choisir de nouveau via la fenêtre native un
    dossier déjà ouvert (ou un dossier sous un chemin enregistré) réutilise
    l'entrée existante au lieu d'échouer.
    """
    try:
        cible = Path(texte).resolve()
    except (OSError, ValueError):
        return None
    for identifiant, entree in _charger().items():
        chemin = _chemin_entree(entree)
        if chemin is not None and chemin == cible:
            return {"id": identifiant, "nom": chemin.name, "chemin": str(chemin)}
    return None


def ajouter_dossier_direct(texte: str, nb_fichiers: int | None = None) -> dict:
    """Valide *texte*, enregistre le dossier et retourne {id, nom, chemin}.

    Idempotent : si le dossier (chemin résolu identique) est déjà au registre,
    retourne l'entrée EXISTANTE au lieu de lever (choisir de nouveau un dossier
    déjà ouvert via la fenêtre native = le resélectionner, pas une erreur).
    Ne copie rien : le dossier réel est simplement référencé.

    `nb_fichiers` (compte au moment de l'ouverture) est mémorisé dans le registre
    pour que le LISTING ne rescanne jamais le disque (il peut devenir périmé mais
    l'ouverture du projet le rafraîchit).
    """
    chemin, nom = valider_chemin_local(texte)
    existant = trouver_par_chemin(str(chemin))
    if existant:
        if nb_fichiers is not None:
            _maj_nb_fichiers(existant["id"], nb_fichiers)
        return existant
    with _VERROU:
        projets = _charger()
        identifiant = id_disponible(nom)
        if identifiant in projets:
            raise ValueError(f"Projet déjà ouvert : {identifiant} ({chemin})")
        entree = {
            "chemin": str(chemin),
            "origine": "dossier",
        }
        if nb_fichiers is not None:
            entree["nb_fichiers"] = nb_fichiers
        projets[identifiant] = entree
        _sauver(projets)
    return {"id": identifiant, "nom": chemin.name, "chemin": str(chemin)}


def _maj_nb_fichiers(identifiant: str, nb_fichiers: int) -> None:
    """Met à jour le compte mémorisé d'une entrée existante (idempotence)."""
    with _VERROU:
        projets = _charger()
        if identifiant in projets:
            projets[identifiant]["nb_fichiers"] = nb_fichiers
            _sauver(projets)


def projets_directs() -> list[dict]:
    """Liste triée des entrées du registre : [{id, nom, chemin, origine, nb_fichiers}]."""
    entrees = []
    for identifiant, entree in sorted(_charger().items()):
        chemin = _chemin_entree(entree)
        if chemin is None:
            continue
        entrees.append({
            "id": identifiant,
            "nom": entree.get("nom") or chemin.name,
            "chemin": str(chemin),
            "origine": "dossier",
            "nb_fichiers": entree.get("nb_fichiers") or 0,
        })
    return entrees


def racine_directe(identifiant: str) -> Path | None:
    """Chemin résolu du dossier direct *identifiant*, sinon None (pas du registre).

    Si le dossier a disparu entre deux appels, retourne None pour échec propre
    (le code appelant lèvera FileNotFoundError là où c'est attendu).
    """
    entree = _charger().get(str(identifiant))
    chemin = _chemin_entree(entree) if entree else None
    if chemin is None or not chemin.is_dir():
        return None
    return chemin


def est_projet_direct(identifiant: str) -> bool:
    return bool(_charger().get(str(identifiant)))