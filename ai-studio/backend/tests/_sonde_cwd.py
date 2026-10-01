"""Le cwd declenche-t-il le ServeError ?

    python tests/_sonde_cwd.py
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import opencode as oc  # noqa: E402

CAS = [
    ("backend (cwd du backend)", Path(r"C:\Users\Roch\Videos\ai-study-assistant\ai-studio\backend")),
    ("projet demo", Path(r"C:\Users\Roch\Videos\ai-study-assistant\ai-studio\data\projets\demo")),
    ("projet demo (sans les 2 fichiers ajoutes)", None),
]


def port_libre(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def tester(cwd: Path, port: int) -> tuple[bool, str]:
    proc = subprocess.Popen(
        [oc._serveur_binaire(), "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace", env=oc._env_serveur(True),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    sortie = []
    try:
        for _ in range(30):
            time.sleep(0.5)
            if not port_libre(port):
                return True, "ecoute"
            if proc.poll() is not None:
                break
        try:
            sortie = (proc.stdout.read() if proc.stdout else "").strip().splitlines()
        except Exception:  # noqa: BLE001
            pass
        detail = next(
            (l for l in sortie if "Error" in l or "error" in l), (sortie or ["(rien)"])[-1]
        )
        return False, detail.strip()[:70]
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


port = 4601
for libelle, cwd in CAS:
    if cwd is None:
        continue
    port += 1
    ok, info = tester(cwd, port)
    print(f"  {libelle:<46} -> {'OK     ' if ok else 'ECHEC  '} {info}")
