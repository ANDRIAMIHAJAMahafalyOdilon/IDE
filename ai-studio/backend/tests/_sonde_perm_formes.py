"""Quelle FORME de `permission.bash` le binaire 1.18.34 applique-t-il vraiment ?

    python tests/_sonde_perm_formes.py

Compare la forme chaîne et la forme objet sur un `git status` anodin, puis sur
`rm`, dans un dossier jetable. Ne modifie AUCUN fichier de l'utilisateur : la
config est écrite dans un dossier temporaire et OPENCODE_CONFIG pointe dessus.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EXE = Path(
    os.environ.get("APPDATA", "")
) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"

CAS = {
    "chaine_ask": '"bash": "ask"',
    "objet_melange": '"bash": { "git status": "allow", "*": "ask" }',
    "objet_etoile_seule": '"bash": { "*": "ask" }',
    "objet_rm_deny": '"bash": { "rm *": "deny", "*": "allow" }',
}

SOCLE = '{"$schema":"https://opencode.ai/config.json","model":"opencode/big-pickle"}'


def port_libre(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def construire(dossier: Path, permission: str) -> Path:
    cfg = dossier / "opencode.jsonc"
    corps = json.loads(SOCLE)
    # Insertion brute : la cle racine "permission" n'existe pas encore dans SOCLE.
    cfg.write_text(
        SOCLE[:-1].rstrip().rstrip(",")
        + ', "permission": {'
        + permission
        + ', "task": "deny" }}',
        encoding="utf-8",
    )
    return cfg


def tester(dossier: Path, cfg: Path, port: int) -> str:
    env = dict(os.environ)
    env.update({"OPENCODE_CONFIG": str(cfg), "AISTUDIO_AGENT": "1"})
    proc = subprocess.Popen(
        [str(EXE), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        env=env, cwd=str(dossier), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", stdin=subprocess.DEVNULL,
    )
    try:
        for _ in range(24):
            time.sleep(0.5)
            if proc.poll() is not None:
                sortie = proc.stdout.read() if proc.stdout else ""
                return "MORT: " + (sortie.strip().splitlines() or ["?"])[-1][:80]
            if not port_libre(port):
                return "ok"
        return "pas de reponse"
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


if not EXE.is_file():
    raise SystemExit(f"binaire introuvable : {EXE}")

port = 4711
with tempfile.TemporaryDirectory() as tmp:
    for nom, permission in CAS.items():
        dossier = Path(tmp) / nom
        dossier.mkdir()
        cfg = construire(dossier, permission)
        port += 1
        etat = tester(dossier, cfg, port)
        print(f"  {nom:<18} {permission:<44} -> {etat}")
        print(f"      config ecrite : {cfg.read_text(encoding='utf-8')}")
