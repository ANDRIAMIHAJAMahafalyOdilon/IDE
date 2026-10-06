"""Point d'entrée de l'application AI Studio en mode exécutable Windows.

Le backend n'expose que des endpoints et la SPA : il n'y a ni fenêtre ni
TrayIcon. Ce lanceur fait le tour du propriétaire absent :

  1. choisit un port libre (8010 est souvent déjà pris par le serveur de dev) ;
  2. démarre uvicorn DANS le processus, dans un thread ;
  3. ouvre le navigateur une fois l'app réellement prête ;
  4. reste vivant jusqu'à la fermeture, puis arrête uvicorn proprement.

PyInstaller onefile extrait dans un dossier temporaire supprimé à la sortie : les
données ne doivent donc JAMAIS y écrire. `app.config` s'en charge (voir BUNDLE_DIR
et DATA_DIR) — ce fichier ne fait qu'orchestrer le démarrage.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def installer_binaire_opencode() -> None:
    """Dépose la CLI OpenCode embarquée et pointe OPENCODE_BIN dessus.

    Doit passer AVANT l'import de `app.config` : `OPENCODE_BIN` y est lu au
    moment de l'import, donc une valeur posée plus tard serait ignorée et
    `resoudre_binaire()` retomberait sur le PATH.
    """
    if not getattr(sys, "frozen", False):
        return
    bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "opencode.exe"
    if not bundle.is_file():
        return
    racine = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AIStudio"
    cible = racine / "bin" / "opencode.exe"
    try:
        if cible.is_file() and cible.stat().st_size == bundle.stat().st_size:
            os.environ["OPENCODE_BIN"] = str(cible)
            return
        cible.parent.mkdir(parents=True, exist_ok=True)
        temp = cible.with_suffix(".tmp")
        shutil.copy2(bundle, temp)
        os.replace(temp, cible)
        os.environ["OPENCODE_BIN"] = str(cible)
    except OSError as exc:
        # Le backend doit rester autonome même si l'antivirus ou une stratégie
        # de poste interdit l'écriture dans %LOCALAPPDATA%. Le binaire est déjà
        # dans le dossier temporaire de l'exécutable one-file : il est lisible
        # et exécutable pendant toute la durée du processus.
        os.environ["OPENCODE_BIN"] = str(bundle)
        print(
            f"CLI OpenCode non extraite ({exc}) : utilisation du binaire embarqué."
        )


installer_binaire_opencode()

# `app.config` doit être importé AVANT uvicorn/app pour que les chemins soient
# posés (et les dossiers de données créés) avant tout accès disque.
from app.config import DATA_DIR, LOGS_DIR  # noqa: E402,F401

SERVEUR = "127.0.0.1"
PORT_PREFERE = 8010
TEMPS_MAX_DEMARRAGE = 30.0


def port_libre(prefere: int = PORT_PREFERE) -> int:
    """Renvoie le premier port libre, en remontant depuis *prefere*.

    On évite `port=0` : un port éphémère change à chaque lancement, ce qui
    cassait les marque-pages et l'historique du navigateur.
    """
    for candidat in range(prefere, prefere + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # SO_EXCLUSIVEADDRUSE : sans lui, Windows peut laisser ce socket
            # apparently lié alors qu'un autre processus l'occupe vraiment.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                s.bind((SERVEUR, candidat))
            except OSError:
                continue
            return candidat
    raise RuntimeError(
        f"Aucun port libre entre {prefere} et {prefere + 50}."
    )


def _attendre_pret(port: int, delai_max: float = TEMPS_MAX_DEMARRAGE) -> bool:
    """Sonde /health en boucle jusqu'à ce que l'app réponde."""
    import urllib.error
    import urllib.request

    fin = time.monotonic() + delai_max
    while time.monotonic() < fin:
        try:
            with urllib.request.urlopen(f"http://{SERVEUR}:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def installer_plugin_arriere_plan() -> None:
    """Dépose le plugin OpenCode qui passe les commandes serveur en arrière-plan.

    Le plugin est régénéré à chaque démarrage : il est la graine de la
    correction « npm run dev bloque », donc il ne doit jamais diverger de la
    version embarquée dans l'exécutable.
    """
    from app.services.agent_fond import installer

    if installer() is not None:
        print("Plugin 'arrière-plan' installé pour l'agent.")


def demarrer(port: int) -> tuple[object, threading.Thread]:
    """Lance uvicorn dans un thread daemon et retourne (config, thread)."""
    import uvicorn

    from app.main import app

    config = uvicorn.Config(
        app,
        host=SERVEUR,
        port=port,
        # "info" et non "warning" : le journal est le seul canal de diagnostic
        # d'un exécutable sans console, on ne doit rien perdre.
        log_level="info",
        access_log=False,
    )
    serveur = uvicorn.Server(config)
    # NE PAS mettre `should_exit = True` ici : cette variable est l'ordre
    # d'arrêt d'uvicorn. La positionner avant le démarrage fait sortir le serveur
    # dès qu'il se lève — on se retrouve avec une app « démarrée » morte en
    # moins d'une seconde. uvicorn la passe à True sur Ctrl+C/SIGTERM, et le
    # `finally` de main() la force à l'arrêtprogrammatique.
    thread = threading.Thread(target=serveur.run, name="uvicorn", daemon=True)
    thread.start()
    return serveur, thread


def _brancher_journaux() -> None:
    """Envoie stdout/stderr et les logs uvicorn dans un fichier rotatif.

    Un exécutable `--noconsole` n'a pas de console : sans ça, la moindre erreur
    de démarrage disparaît. Le fichier est borné — un plantage en boucle ne doit
    pas remplir le disque.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    # Le dossier peut ne pas encore exister : `assurer_repertoires()` n'est
    # appelé qu'au démarrage de l'app, or c'est le lanceur qui journalise en
    # premier. Sans ce mkdir, RotatingFileHandler lève et le lanceur meurt muet.
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fichier = LOGS_DIR / "aistudio.log"
    gestionnaire = RotatingFileHandler(
        fichier, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    gestionnaire.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[gestionnaire])
    # stdout/stderr n'existent pas en mode fenêtré : on les remplace pour que les
    # `print()` de ce module atterrissent dans le fichier et ne lèvent pas.
    for flux in (sys.stdout, sys.stderr):
        if flux is not None:
            flux.close()
    sys.stdout = open(fichier, "a", encoding="utf-8")
    sys.stderr = sys.stdout


def main() -> int:
    _brancher_journaux()
    installer_plugin_arriere_plan()
    try:
        port = port_libre()
    except RuntimeError as exc:
        print(f"Impossible de démarrer : {exc}", file=sys.stderr)
        return 1

    serveur, _thread = demarrer(port)
    print(f"Port choisi : {port} — démarrage du serveur…", flush=True)

    if not _attendre_pret(port):
        print(
            f"Le serveur n'a pas démarré dans le temps imparti "
            f"(started={serveur.started}, should_exit={serveur.should_exit}).",
            flush=True,
        )
        serveur.should_exit = True
        return 1

    adresse = f"http://{SERVEUR}:{port}"
    print(f"AI Studio — {adresse}")
    webbrowser.open(adresse)

    # `should_exit` est positionné par un signal d'arrêt ; on peut aussi fermer
    # simplement la fenêtre du navigateur, auquel cas l'exécutable reste vivant
    # (comportement voulu : il EST l'app).
    try:
        while not serveur.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
    finally:
        serveur.should_exit = True
        _thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        # Dernier filet : un exécutable sans console n'a rien d'autre où dire
        # qu'il aplanté. On tente le journal, sinon un fichier à côté de l'exe.
        import traceback

        detail = traceback.format_exc()
        try:
            (LOGS_DIR / "demarrage-erreur.log").write_text(detail, encoding="utf-8")
        except OSError:
            try:
                from pathlib import Path

                Path(sys.executable).with_suffix(".erreur.log").write_text(
                    detail, encoding="utf-8"
                )
            except OSError:
                pass
        raise
