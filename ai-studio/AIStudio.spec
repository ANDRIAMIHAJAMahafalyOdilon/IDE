# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller — AI Studio en UN seul .exe (onefile).

Ce que le .exe embarque :
  * le backend (FastAPI + uvicorn + ses dépendances) ;
  * le build Vite du frontend (`frontend/dist`), servi par le backend ;
  * `opencode.jsonc` : la config doit exister SUR DISQUE et lisible, OpenCode la
    recevant par OPENCODE_CONFIG. On ne peut pas la laisser en lecture seule
    dans le zip PyInstaller ;
  * la CLI `opencode.exe` : ~172 Mo, qui fait la taille de l'exécutable. C'est
    le prix de « copier un fichier et ça marche ». Elle est EXTRAITE dans
    %LOCALAPPDATA%\\AIStudio\\bin au premier lancement et visée par OPENCODE_BIN
    — voir `lancer.py:installer_binaire_opencode`.

Ce que le .exe n'embarque PAS, volontairement :
  * le `.env` : ce sont des SECRETS. Un `.env` dans le .exe serait extractible
    par n'importe qui (`strings`, décompresseur onefile). Il est lu depuis
    %APPDATA%\\AIStudio\\.env.
  * les données : elles vivent dans %LOCALAPPDATA%\\AIStudio\\data. Le onefile
    extrait dans un dossier temporaire effacé à la fermeture — y écrire
    condamnerait l'utilisateur à tout perdre à chaque lancement.

PRÉREQUIS RESTANT : Node.js (pour les projets de l'utilisateur et la résolution
`npm prefix -g`) et PowerShell 7 (`winget install Microsoft.PowerShell`) pour le
terminal intégré. `opencode auth login` reste à faire une fois sur la machine
cible : le credential est un secret, il ne peut pas vivre dans l'exécutable.
"""

import os
import shutil
import subprocess
from pathlib import Path

RACINE = Path(SPECPATH).resolve()  # dossier contenant ce .spec
DIST = RACINE / "frontend" / "dist"

if not (DIST / "index.html").is_file():
    raise SystemExit(
        f"Build frontend absent ({DIST / 'index.html'}).\n"
        "Lance d'abord :  cd ai-studio\\frontend && npm run build"
    )


def _resoudre_cli_opencode() -> Path:
    """Le `opencode.exe` à embarquer, ou SystemExit.

    Résolu au BUILD, pas à l'exécution : l'exe embarqué doit être la version
    testée. `OPENCODE_EXE` permet de pointer une autre installation.
    """
    impose = os.environ.get("OPENCODE_EXE", "")
    if impose:
        if not Path(impose).is_file():
            raise SystemExit(f"OPENCODE_EXE introuvable : {impose}")
        return Path(impose)
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        try:
            prefix = subprocess.run(
                [npm, "prefix", "-g"], capture_output=True, text=True, timeout=60
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            prefix = ""
        if prefix:
            candidat = Path(prefix) / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            if candidat.is_file():
                return candidat
    trouve = shutil.which("opencode")
    if trouve:
        candidat = Path(trouve)
        if candidat.suffix.lower() == ".exe" and candidat.is_file():
            return candidat
        if candidat.suffix.lower() in {".cmd", ".ps1", ""}:
            reel = candidat.parent.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            if reel.is_file():
                return reel
    raise SystemExit(
        "CLI OpenCode introuvable pour l'embarquement.\n"
        "Installe-la d'abord (`npm i -g opencode-ai`) ou renseigne OPENCODE_EXE."
    )


CLI_OPENCODE = _resoudre_cli_opencode()
print(f"CLI OpenCode embarquee : {CLI_OPENCODE} "
      f"({CLI_OPENCODE.stat().st_size / (1024 * 1024):.0f} Mo)")

# (source, dossier de destination DANS le .exe) — même convention que
# app/config.py.
#
# ATTENTION : le second élément est un DOSSIER, pas un chemin complet. Écrire
# ("…/opencode.jsonc", "opencode.jsonc") crée un DOSSIER nommé opencode.jsonc
# contenant le vrai fichier : l'agent accepte alors un chemin qui est un
# répertoire et échoue sur "BadResource: FileSystem.readFile". Pour un fichier,
# la destination est donc le point, soit la racine du bundle.
donnees = [
    (str(DIST), "frontend/dist"),
    (str(RACINE / "opencode.jsonc"), "."),
    # La CLI est un binaire, pas une donnée : elle part dans `binaries` pour
    # qu'elle garde ses attributs exécutables. Racine du bundle, même convention
    # que `opencode.jsonc` — voir `lancer.py:installer_binaire_opencode`.
]
binaires = [
    (str(CLI_OPENCODE), "."),
]

# Dépendances importées dynamiquement ou via plugins que l'analyse statique ne
# voit pas (les boucles uvicorn, le backend anyio, ...).
imports_cache = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
    # Importé DANS une fonction de lancer.py : l'analyse statique peut le rater,
    # et sans lui l'exe n'écrit ni plugin ni lanceur, donc plus rien ne part en
    # arrière-plan alors que tout le reste semble normal.
    "app.services.agent_fond",
]

a = Analysis(
    [str(RACINE / "backend" / "lancer.py")],
    pathex=[str(RACINE / "backend")],
    binaries=binaires,
    datas=donnees,
    hiddenimports=imports_cache,
    hookspath=[],
    runtime_hooks=[],
    # On exclut les poids morts qui gonfleraient le .exe pour rien.
    excludes=[
        "tkinter.test", "test", "unittest", "pydoc_data",
        "matplotlib", "numpy.f2py", "IPython", "notebook",
        "sqlite3.test",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AIStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX fait planter des antivirus sans vraiment gagner grand-chose
    runtime_tmpdir=None,
    # onefile : tout est dans UN exécutable. Contrepartie acceptée : le
    # démarrage décompresse ~400 Mo dans %TEMP% (le backend + la CLI OpenCode),
    # soit 30 à 90 s au premier lancement. Les lancements suivants profitent du
    # cache Windows, et la CLI n'est réécrite dans %LOCALAPPDATA% qu'une fois.
    console=False,      # pas de fenêtre noire : l'app EST la fenêtre du navigateur
    disable_windowed_traceback=False,
    icon=None,
    upx_exclude=[],
    # Un nom de fichier sans espace évite les soucis d'antivirus.
    name_suffix="",
)
