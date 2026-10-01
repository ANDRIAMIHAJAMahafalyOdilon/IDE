"""Sonde brute : quels types d'evenements OpenCode 4097 envoie-t-il vraiment ?

Reproduit exactement le protocole de services/opencode.py:flux_tache
(GET /event + POST /session/{sid}/prompt_async).

    python tests/_sonde_permission_brute.py <projet>
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.services import opencode as oc  # noqa: E402

PROJET = sys.argv[1] if len(sys.argv) > 1 else "demo"
RACINE = Path(r"C:\Users\Roch\Videos\ai-study-assistant\ai-studio\data\projets") / PROJET
BASE = oc.OPENCODE_AGENT_BASE_URL

permission_brute: list[dict] = []
outils_vus: list[dict] = []
verrou = threading.Lock()


def _repondre() -> None:
    limite = time.monotonic() + 300
    while time.monotonic() < limite:
        with verrou:
            if permission_brute:
                break
        time.sleep(0.2)
    with verrou:
        if not permission_brute:
            print("\n>>> AUCUN evenement de permission sur le fil brut")
            return
        props = permission_brute[0].get("properties") or {}
    rid = props.get("id") or props.get("requestID") or permission_brute[0].get("id")
    print(f"\n>>> id={rid}")
    try:
        oc.repondre_permission(RACINE, str(rid), "once", agent=True)
        print(">>> repondre_permission('once') : OK")
    except Exception as exc:  # noqa: BLE001
        print(f">>> repondre_permission ECHEC : {type(exc).__name__}: {exc}")


threading.Thread(target=_repondre, daemon=True).start()

oc.assurer_serveur_agent(RACINE)
print(f"serveur agent : {BASE}")
print(f"cwd projet    : {RACINE}")

sid = oc.creer_session(RACINE, "Lance `npm run dev` puis dis-moi quand c'est demarre.", agent=True)
print(f"session       : {sid}\n")

racine, params = oc._serveur_params(RACINE)
headers = {"x-opencode-directory": racine}
types_vus: dict[str, int] = {}

with httpx.Client(base_url=BASE.rstrip("/"), timeout=httpx.Timeout(300.0, read=300.0)) as client:
    with client.stream("GET", "/event", params=params, headers=headers) as flux:
        rep = client.post(
            f"/session/{sid}/prompt_async", params=params, headers=headers,
            json={"parts": [{"type": "text", "text": "Lance le projet avec `npm run dev`."}]},
        )
        print(f"prompt_async -> HTTP {rep.status_code} {rep.text[:160]}\n")
        if rep.status_code >= 400:
            raise SystemExit("la tache a ete refusee")

        data_brutes: list[str] = []
        for ligne in flux.iter_lines():
            if ligne.startswith("data:"):
                data_brutes.append(ligne[5:].strip())
                continue
            if ligne.strip() or not data_brutes:
                continue
            brut = "\n".join(data_brutes)
            data_brutes = []
            try:
                obj = oc._sse_payload(brut)
            except Exception:  # noqa: BLE001
                continue
            if obj is None:
                continue
            typ = obj.get("type") or "?"
            types_vus[typ] = types_vus.get(typ, 0) + 1
            props = obj.get("properties") or {}
            if typ.startswith("permission"):
                with verrou:
                    permission_brute.append(obj)
                print(f"  >>> {typ} : {json.dumps(props, ensure_ascii=False)[:280]}")
            elif typ in ("message.part.updated", "message.updated"):
                part = props.get("part") or {}
                if part.get("type") == "tool":
                    outils_vus.append(part)
                    etat = part.get("state") or {}
                    print(f"  [outil] {part.get('tool')} status={etat.get('status')} "
                          f"{json.dumps(etat.get('input'), ensure_ascii=False)[:120]}")
            elif typ == "session.error":
                print(f"  [session.error] {json.dumps(props, ensure_ascii=False)[:220]}")

print("\n=== TYPES D'EVENEMENTS SUR LE FIL BRUT ===")
for typ, n in sorted(types_vus.items(), key=lambda kv: -kv[1]):
    print(f"  {n:>4} x {typ}")
print("\npermission sur le fil brut :", "OUI" if permission_brute else "NON")
print("outils vus                  :", len(outils_vus))
for o in outils_vus[:6]:
    etat = o.get("state") or {}
    print(f"   - {o.get('tool')} : {etat.get('status')} : "
          f"{json.dumps(etat.get('input'), ensure_ascii=False)[:130]}")
