"""Sonde : bash en ask -> permission -> le bouton fait avancer la session.

    python tests/_decisif_permission.py [port]

PAS un test unitaire : exige un backend DÉJÀ lancé et l'agent réel. D'où le
préfixe `_`, comme les autres sondes du dossier.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import httpx

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8011
BASE = f"http://127.0.0.1:{PORT}"
PROJET = "demo"

vu: dict | None = None
rep_envoyee = threading.Event()
verrou = threading.Lock()


def _repondre() -> None:
    """Répond 'once' dès qu'une permission est captée, pendant que le flux court."""
    global vu
    limite = time.monotonic() + 420
    while time.monotonic() < limite:
        with verrou:
            if vu:
                break
        time.sleep(0.2)
    with verrou:
        if not vu:
            print("\n>>> aucune permission recue : rien a repondre")
            return
        rid = vu.get("id")
    print(f"\n>>> permission recue, id={rid}")
    print(f">>> payload : {json.dumps(vu, ensure_ascii=False)[:300]}")
    try:
        r = httpx.post(
            f"{BASE}/api/agent/tache/permission",
            json={"projet": PROJET, "request_id": rid, "reply": "once"},
            timeout=20,
        )
        print(f">>> reponse 'once' : HTTP {r.status_code} {r.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f">>> reponse 'once' ECHEC : {type(exc).__name__}: {exc}")
    rep_envoyee.set()


def main() -> int:
    noms: list[str] = []
    compte: dict[str, int] = {}
    permission_trouvee = False
    outil = False
    texte_apres_permission = False
    termine = "non"

    threading.Thread(target=_repondre, daemon=True).start()

    corps = {
        "projet": PROJET,
        "message": "Lance le projet avec `npm run dev` puis dis-moi quand c'est demarre.",
        "session": f"test-perm-{int(time.time())}",
    }
    print(f"POST /api/agent/tache : {corps['message']}\n")

    evenement_courant = "message"
    with httpx.stream(
        "POST", f"{BASE}/api/agent/tache", json=corps,
        timeout=httpx.Timeout(480.0, read=480.0),
    ) as flux:
        for ligne in flux.iter_lines():
            if not ligne:
                continue
            if ligne.startswith(":"):
                continue
            if ligne.startswith("event:"):
                evenement_courant = ligne[6:].strip()
                continue
            if not ligne.startswith("data:"):
                continue
            data = ligne[5:].strip()
            nom = evenement_courant
            noms.append(nom)
            compte[nom] = compte.get(nom, 0) + 1
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001
                print(f"  [{nom}] <non-json> {data[:160]}")
                continue

            if nom == "permission":
                if obj.get("status") == "waiting_for_permission":
                    global vu
                    with verrou:
                        vu = obj
                    permission_trouvee = True
                    print(f"  >>> PERMISSION {json.dumps(obj, ensure_ascii=False)[:250]}")
                else:
                    print(f"  [permission] {json.dumps(obj, ensure_ascii=False)[:200]}")
            elif nom == "activite":
                t = obj.get("type")
                if t in ("bash", "terminal", "shell"):
                    outil = True
                print(f"  [activite:{t}] {str(obj.get('title'))[:70]}")
            elif nom == "texte":
                t = str(obj.get("texte") or obj.get("delta") or "")
                if permission_trouvee and t.strip():
                    texte_apres_permission = True
                    print(f"  [texte APRES PERMISSION] {t[:160]}")
            elif nom in ("erreur", "fin"):
                print(f"  [{nom}] {json.dumps(obj, ensure_ascii=False)[:280]}")
                if nom == "erreur":
                    termine = "erreur"
                break

    print("\n=== BILAN ===")
    print("evenements par type :", compte)
    print("permission recue    :", "OUI" if permission_trouvee else "NON")
    print("outil bash vu       :", "OUI" if outil else "NON")
    print("reponse 'once'      :", "OUI" if rep_envoyee.is_set() else "NON")
    print("texte apres perm    :", "OUI" if texte_apres_permission else "NON")
    print("fin du flux         :", termine)
    return 0 if permission_trouvee else 1


if __name__ == "__main__":
    raise SystemExit(main())
