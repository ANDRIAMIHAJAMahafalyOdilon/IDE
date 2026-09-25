"""Tests du terminal intégré (WebSocket) contre un vrai serveur uvicorn.

Le terminal est un vrai pwsh dont le répertoire de travail est la racine du
projet. Testé en conditions réelles (pas de TestClient) — pattern du projet.
     python tests/test_terminal.py
Ignoré proprement si pwsh est absent.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SUITE_DIR = pathlib.Path(__file__).resolve().parent
BACKEND = SUITE_DIR.parent
PY = sys.executable

HOTE = "127.0.0.1"
PORT = 8012
BASE = f"http://{HOTE}:{PORT}"


def _attendre_health(proc, essais=60):
    for _ in range(essais):
        if proc.poll() is not None:
            raise RuntimeError("uvicorn est mort en démarrant")
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=5) as rep:
                if rep.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.5)
    raise RuntimeError("uvicorn ne répond pas")


async def _envoyer_et_voir(port: int, projet: str, commande: str, attend: str, essais: float = 45):
    import websockets
    import websockets.exceptions

    uri = f"ws://{HOTE}:{port}/api/ws/terminal?projet={projet}"
    async with websockets.connect(uri) as ws:
        banniere = ""
        for _ in range(8):
            try:
                banniere += await asyncio.wait_for(ws.recv(), timeout=1.2)
            except asyncio.TimeoutError:
                pass
            if "répertoire" in banniere:
                break
        assert "répertoire" in banniere, f"bannière absente : {banniere[:120]!r}"
        await ws.send(commande)
        total = ""
        en_attente = essais
        while en_attente > 0:
            try:
                total += await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                en_attente -= 1
            if attend in total:
                return
        raise AssertionError(f"le terminal n'a pas renvoyé {attend!r} ; reçu : {total[:200]!r}")


async def _projet_inconnu(port: int):
    import websockets.exceptions

    try:
        async with websockets.connect(f"ws://{HOTE}:{port}/api/ws/terminal?projet=absent"):
            raise AssertionError("connexion attendue refusée")
    except websockets.exceptions.InvalidStatus as exc:
        assert exc.response.status_code == 403  # uvicorn : handshake refusé = 403


def test_ws_terminal_execute_commande():
    if not shutil.which("pwsh"):
        print("  [IGNORÉ] pwsh introuvable — test terminal réel sauté")
        return
    with tempfile.TemporaryDirectory() as tmp:
        projets = pathlib.Path(tmp) / "projets"
        (projets / "demo").mkdir(parents=True)
        env = dict(os.environ)
        env["PROJETS_DIR"] = str(projets)
        serveur = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app", "--host", HOTE, "--port", str(PORT)],
            cwd=str(BACKEND),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _attendre_health(serveur)
            asyncio.run(_envoyer_et_voir(PORT, "demo", "Write-Output 'coucou-pwsh'\n", "coucou-pwsh"))
            asyncio.run(_projet_inconnu(PORT))
        finally:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(serveur.pid)],
                    capture_output=True,
                )
            else:
                serveur.terminate()
                try:
                    serveur.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    serveur.kill()


def _tout_executer():
    tests = sorted(
        (nom, obj) for nom, obj in globals().items()
        if nom.startswith("test_") and callable(obj)
    )
    nb_ok = 0
    for nom, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  [E] {nom} : {exc!r}")
        else:
            nb_ok += 1
            print(f"  [OK] {nom}")
    print(f"\n{nb_ok}/{len(tests)} tests réussis")
    return 0 if nb_ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_tout_executer())