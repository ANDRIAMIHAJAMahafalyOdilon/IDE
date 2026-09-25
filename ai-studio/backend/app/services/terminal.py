"""Terminal intégré : un PowerShell persistant par projet, connecté en WebSocket.

Chaque projet a un unique processus `pwsh` dont le répertoire de travail est la
racine du projet. Un thread lit stdout/stderr (fusionnés) et pousse les lignes
dans une file asyncio ; le flux WebSocket les relaie au frontend, qui écrit ses
commandes via stdin. En cas de reconnexion, le terminal du projet est réutilisé
(la session survit à la fermeture du WS).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from pathlib import Path


class TerminalIndisponible(RuntimeError):
    """pwsh manquant, ou projet invalide."""


class TerminalSession:
    """Couche process + file de sortie pour un projet."""

    def __init__(self, racine: Path):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise TerminalIndisponible(
                "PowerShell 7 (pwsh) introuvable dans le PATH du backend."
            )
        env = os.environ.copy()
        # Sortie du processus en UTF-8 (PowerShell 7 est UTF-8 natif).
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._racine = racine
        self._proc = subprocess.Popen(
            [pwsh, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"],
            cwd=racine,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._file_sortie: list[str] = []
        self._verrou = threading.Lock()
        self._lecteur = threading.Thread(
            target=self._lire, name=f"term-{racine.name}", daemon=True
        )
        self._lecteur.start()

    @property
    def racine(self) -> Path:
        return self._racine

    @property
    def actif(self) -> bool:
        return self._proc.poll() is None

    def banniere(self) -> str:
        return (
            f"AI Studio · terminal PowerShell\n"
            f"répertoire : {self._racine}\n"
            "— entre tes commandes (Ctrl+C, Ctrl+L…).\n"
        )

    def _lire(self) -> None:
        """Passeur thread -> file (lecture bloquante de stdout)."""
        try:
            for ligne in iter(self._proc.stdout.readline, ""):
                with self._verrou:
                    self._file_sortie.append(ligne)
        except Exception:  # noqa: BLE001 — stdout fermé = fin du process
            pass

    async def prochaine_ligne(self) -> str:
        """Retourne la prochaine ligne de sortie (bloquant), ou la bannière."""
        while True:
            with self._verrou:
                if self._file_sortie:
                    return self._file_sortie.pop(0)
            if not self.actif:
                return ""
            await asyncio.sleep(0.02)

    async def envoyer(self, texte: str) -> None:
        """Écrit une commande sur stdin (depuis un WS)."""
        if not self.actif:
            raise TerminalIndisponible("Terminal arrêté.")
        await asyncio.to_thread(self._ecrire, texte)

    def _ecrire(self, texte: str) -> None:
        try:
            self._proc.stdin.write(texte)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def fermer(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
        except OSError:
            pass


# ─────────────────────────── Registry par projet ───────────────────────────

_TERMINAUX: dict[str, TerminalSession] = {}
_VERROU: threading.Lock = threading.Lock()


def obtenir_terminal(nom_projet: str, racine: Path) -> TerminalSession:
    """Retourne la session du projet (créée si besoin), thread-safe."""
    with _VERROU:
        session = _TERMINAUX.get(nom_projet)
        if session is not None and session.actif and session.racine == racine:
            return session
        session = TerminalSession(racine)
        _TERMINAUX[nom_projet] = session
        return session


def fermer_terminal(nom_projet: str) -> None:
    with _VERROU:
        session = _TERMINAUX.pop(nom_projet, None)
        if session:
            session.fermer()


def arreter_tous() -> None:
    with _VERROU:
        for session in _TERMINAUX.values():
            session.fermer()
        _TERMINAUX.clear()