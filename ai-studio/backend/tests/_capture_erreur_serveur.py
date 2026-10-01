"""Reproduit _demarrer_serveur en capturant stderr, que le code jette (DEVNULL).

    python tests/_capture_erreur_serveur.py <projet> [agent]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import opencode as oc  # noqa: E402

PROJET = sys.argv[1] if len(sys.argv) > 1 else "demo"
AGENT = (sys.argv[2] if len(sys.argv) > 2 else "agent") == "agent"
RACINE = Path(r"C:\Users\Roch\Videos\ai-study-assistant\ai-studio\data\projets") / PROJET
PORT = 4097 if AGENT else 4096

print(f"binaire   : {oc._serveur_binaire()}")
print(f"racine    : {RACINE}")
print(f"port      : {PORT}")
print(f"OPENCODE_CONFIG : {oc.OPENCODE_CONFIG}")

env = oc._env_serveur(AGENT)
print(f"AISTUDIO_AGENT dans l'env : {env.get('AISTUDIO_AGENT')}")

proc = subprocess.Popen(
    [oc._serveur_binaire(), "serve", "--hostname", "127.0.0.1", "--port", str(PORT)],
    cwd=str(RACINE),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    errors="replace",
    env=env,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
print("\n--- sortie du processus ---")
try:
    for ligne in proc.stdout:  # type: ignore[union-attr]
        print("  " + ligne.rstrip())
        if "listening" in ligne.lower() or "serve" == ligne.strip()[:5].lower():
            break
except Exception as exc:  # noqa: BLE001
    print(f"  (lecture interrompue : {exc})")

code = proc.poll()
if code is None:
    print("\n  le processus tourne encore -> il demarre")
    proc.kill()
else:
    print(f"\n  code de sortie : {code}")
proc.wait(timeout=5)
print("--- fin ---")
