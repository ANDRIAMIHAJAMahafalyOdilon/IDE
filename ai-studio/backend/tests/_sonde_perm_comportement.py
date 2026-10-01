"""La forme OBJET de `permission.bash` est-elle APPLIQUEE, ou seulement acceptee ?

    python tests/_sonde_perm_comportement.py

Le test precedent (`_sonde_perm_formes.py`) prouve seulement que le serveur
DEMARRE avec les deux formes - ce qui ne dit rien de l'application. Ici on lance
une vraie session qui tente une commande et on regarde si l'agent demande une
permission (`permission.asked`) ou execute sans rien demander.

Cas attendu si la forme objet fonctionne :
  * `git status` en "allow"  -> AUCUNE permission, la commande passe ;
  * meme commande en "ask"    -> permission demandee.
Cas attendu si elle est ignoree : les deux cas se comportent pareil.

Repondu automatiquement a toute permission ("once") pour ne pas rester bloque.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

EXE = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"

CAS = [
    ("chaine_ask", '"bash": "ask"', "echelle de la commande : 1"),
    ("objet_allow_git", '"bash": { "git *": "allow", "*": "ask" }', "echelle de la commande : 1"),
    ("objet_deny_git", '"bash": { "git *": "deny", "*": "allow" }', "echelle de la commande : 1"),
]

SOCLE = '{"$schema":"https://opencode.ai/config.json","model":"opencode/big-pickle","task":"deny"'
CONSIGNE = "Lance exactement cette commande et dis-moi le resultat : `git status`. Ne fais rien d'autre."


def port_libre(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def lancer(dossier: Path, cfg: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"OPENCODE_CONFIG": str(cfg), "AISTUDIO_AGENT": "1"})
    proc = subprocess.Popen(
        [str(EXE), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        env=env, cwd=str(dossier), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(40):
        time.sleep(0.5)
        if not port_libre(port):
            return proc
        if proc.poll() is not None:
            raise SystemExit(f"le serveur n'a pas demarre (code {proc.returncode})")
    raise SystemExit("le serveur n'ecoute pas")


def observer(port: int, dossier: Path, consigne: str) -> dict:
    """Demande, repond automatiquement, et rend ce qui a ete observe."""
    base = f"http://127.0.0.1:{port}"
    params = {"directory": str(dossier.resolve())}
    entetes = {"x-opencode-directory": str(dossier.resolve())}
    vu = {"permission": 0, "bash_run": 0, "bash_error": 0, "texte": 0}

    with httpx.Client(base_url=base, timeout=httpx.Timeout(120.0, read=120.0)) as client:
        r = client.post("/session", params=params, headers=entetes, json={})
        r.raise_for_status()
        sid = r.json()["id"]
        arreter = threading.Event()

        def repondre() -> None:
            while not arreter.is_set():
                time.sleep(0.3)
                try:
                    pend = client.get("/permission", params=params, headers=entetes)
                    for p in pend.json() or []:
                        client.post(
                            f"/permission/{p['id']}/reply", params=params,
                            headers=entetes, json={"response": "once"},
                        )
                except Exception:  # noqa: BLE001 — sonde best-effort
                    pass

        threading.Thread(target=repondre, daemon=True).start()
        with client.stream("GET", "/event", params=params, headers=entetes) as flux:
            client.post(
                f"/session/{sid}/prompt_async", params=params, headers=entetes,
                json={"parts": [{"type": "text", "text": consigne}]},
            )
            brut: list[str] = []
            fin = time.monotonic() + 75
            while time.monotonic() < fin:
                for ligne in flux.iter_lines():
                    if ligne.startswith("data:"):
                        brut.append(ligne[5:].strip())
                        continue
                    if not ligne.strip():
                        if not brut:
                            continue
                        payload = "\n".join(brut)
                        brut = []
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        typ = evt.get("type") or ""
                        props = evt.get("properties") or {}
                        if typ.startswith("permission") and "asked" in typ:
                            vu["permission"] += 1
                        if typ == "message.part.updated":
                            part = props.get("part") or {}
                            if part.get("type") == "tool" and part.get("tool") == "bash":
                                etat = part.get("state") or {}
                                if etat.get("status") == "running":
                                    vu["bash_run"] += 1
                                elif etat.get("status") == "error":
                                    vu["bash_error"] += 1
                            if part.get("type") == "text" and part.get("text"):
                                vu["texte"] += 1
                    if arreter.is_set():
                        break
                break
        arreter.set()
    return vu


if not EXE.is_file():
    raise SystemExit(f"binaire introuvable : {EXE}")

port = 4731
for nom, permission, _ in CAS:
    with tempfile.TemporaryDirectory() as tmp:
        dossier = Path(tmp)
        cfg = dossier / "opencode.jsonc"
        cfg.write_text(SOCLE + ', "permission": {' + permission + "}}", encoding="utf-8")
        port += 1
        proc = lancer(dossier, cfg, port)
        try:
            vu = observer(port, dossier, CONSIGNE)
        finally:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        verdict = (
            "AUTORISE SANS DEMANDE"
            if vu["permission"] == 0 and vu["bash_run"]
            else ("DEMANDE" if vu["permission"] else "AUCUN bash")
        )
        print(f"  {nom:<16} {permission:<40} -> {verdict}   {vu}")
