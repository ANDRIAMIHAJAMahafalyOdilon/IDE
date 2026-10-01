"""Quelle valeur de permission.bash OpenCode 1.18.33 accepte-t-il vraiment ?

    python tests/_sonde_bash_valeurs.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(r"C:\Users\Roch\Videos\ai-study-assistant\ai-studio\opencode.jsonc")
ORIGINAL = CONFIG.read_text(encoding="utf-8")


EXE = Path(
    r"C:\Users\Roch\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe"
)


def port_libre(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def tester(valeur: str, port: int) -> tuple[bool, str]:
    CONFIG.write_text(
        ORIGINAL.replace('"bash": "ask"', f'"bash": "{valeur}"'), encoding="utf-8"
    )
    env = dict(os.environ)
    env.update({
        "OPENCODE_CONFIG": str(CONFIG),
        "AISTUDIO_AGENT": "1",
    })
    proc = subprocess.Popen(
        [str(EXE), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(24):
            time.sleep(0.5)
            if proc.poll() is not None:
                sortie = proc.stdout.read() if proc.stdout else ""
                return False, (sortie.strip().splitlines() or ["(mort)"])[-1][:90]
            if not port_libre(port):
                return True, "demarre"
        return False, "aucune reponse en 12 s"
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


try:
    for valeur in ("allow", "ask", "deny"):
        port = 4500 + hash(valeur) % 90
        ok, info = tester(valeur, port)
        print(f"  bash={valeur:<6} -> {'DEMARRE  ' if ok else 'REFUSE   '} {info}")
finally:
    CONFIG.write_text(ORIGINAL, encoding="utf-8")
    print("\nconfig restauree :", '"bash": "ask"' in CONFIG.read_text(encoding="utf-8"))
