"""E2E bout-en-bout contre un vrai serveur uvicorn (pas TestClient).

Scénario clé demandé : l'utilisateur édite *manuellement* un fichier dans
Monaco (PUT /file) pendant qu'une proposition de diff sur ce même fichier est
en attente -> source_hash périmé -> apply-changes doit bloquer proprement
(statut "erreur" ... périmé) SANS écraser la modification manuelle.

     python tests/e2e_stale.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

SUITE_DIR = pathlib.Path(__file__).resolve().parent
BACKEND = SUITE_DIR.parent
PY = sys.executable

HOTE = "127.0.0.1"
PORT = 8011
BASE = f"http://{HOTE}:{PORT}"

CANONIQUE = (
    "def additionner(a, b):\n    return a + b\n\n\n"
    'if __name__ == "__main__":\n    print(additionner(2, 3))\n'
)


def _req(chemin: str, methode="GET", corps=None):
    data = json.dumps(corps).encode("utf-8") if corps is not None else None
    req = urllib.request.Request(
        BASE + chemin,
        data=data,
        method=methode,
        headers={"Content-Type": "application/json"} if corps is not None else {},
    )
    with urllib.request.urlopen(req, timeout=60) as rep:
        return rep.status, rep.read().decode("utf-8")


def _sse_propositions(projet, message, simulation):
    _, texte = _req(
        "/api/agent/chat", "POST",
        {"projet": projet, "message": message, "simulation": simulation},
    )
    evts = []
    for bloc in texte.replace("\r\n", "\n").split("\n\n"):
        event, data = "message", ""
        for ligne in bloc.splitlines():
            if ligne.startswith("event:"):
                event = ligne[6:].strip()
            elif ligne.startswith("data:"):
                data += ligne[5:].strip()
        if event and data:
            evts.append((event, json.loads(data)))
    return evts


def _attendre_health(proc, essais=60):
    for _ in range(essais):
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn est mort : {proc.stderr.read()}")
        try:
            code, texte = _req("/health")
            if code == 200:
                return
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.5)
    raise RuntimeError("uvicorn ne répond pas")


def main() -> int:
    serveur = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", HOTE, "--port", str(PORT)],
        cwd=str(BACKEND),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _attendre_health(serveur)

        # 0) état canonique
        _, _ = _req("/api/projects/demo/file", "PUT",
                    {"chemin": "main.py", "contenu": CANONIQUE})

        # 1) ouverture du fichier (GET /file, équivalent éditeur)
        code, texte = _req("/api/projects/demo/file?chemin=main.py")
        assert code == 200 and json.loads(texte)["contenu"] == CANONIQUE
        print("[OK] GET /file -> contenu canonique")

        # 2) chat simulé -> proposition (source_hash calculé sur le disque actuel)
        evts = _sse_propositions(
            "demo",
            "passe additionner à 2 additions",
            [{"chemin": "main.py",
              "contenu": "def additionner(a, b):\n"
                         "    return a + b\n\n\n"
                         'if __name__ == "__main__":\n'
                         "    print(additionner(2, 3))\n"
                         "    print(additionner(10, 5))\n"}],
        )
        props = [d for e, d in evts if e == "proposition"]
        assert len(props) == 1, evts
        prop = props[0]
        modifier_manuel = (
            "def additionner(a, b):\n    return a + b\n\n\n"
            "def multiplier(a, b):\n    return a * b\n\n\n"
            'if __name__ == "__main__":\n    print(additionner(2, 3))\n'
            "    print(multiplier(4, 5))\n"
        )
        print("[OK] chat -> proposition source_hash=", prop["source_hash"][:8])

        # 3) édition MANUELLE du fichier dans l'éditeur (PUT /file = Ctrl+S)
        _, _ = _req("/api/projects/demo/file", "PUT",
                    {"chemin": "main.py", "contenu": modifier_manuel})
        print("[OK] PUT /file (édition manuelle Monaco) -> disque modifié")

        # 4) apply de la proposition EN ATTENTE (déjà périmée)
        code, texte = _req("/api/agent/apply-changes", "POST", {
            "projet": "demo",
            "modifications": [{
                "fichier": prop["fichier"],
                "action": prop["action"],
                "source_hash": prop["source_hash"],
                "acceptes": prop["hunks"],
            }],
        })
        assert code == 200, texte
        resultat = json.loads(texte)["resultats"][0]
        assert resultat["statut"] == "erreur", resultat
        assert "périmé" in (resultat["message"] or ""), resultat
        print(f"[OK] apply stale -> {resultat['statut']} : {resultat['message']}")

        # 5) la modification manuelle n'a PAS été écrasée
        _, texte = _req("/api/projects/demo/file?chemin=main.py")
        assert json.loads(texte)["contenu"] == modifier_manuel, "écrasement silencieux !"
        print("[OK] contenu manuel préservé (pas d'écrasement silencieux)")

        # 6) reprise du flux : nouvelle proposition alignée sur le disque -> apply OK
        evts = _sse_propositions(
            "demo",
            "affiche aussi carre(5)",
            [{"chemin": "main.py", "contenu": modifier_manuel + "    print(6 * 9)\n"}],
        )
        prop2 = [d for e, d in evts if e == "proposition"][0]
        _, texte = _req("/api/agent/apply-changes", "POST", {
            "projet": "demo",
            "modifications": [{
                "fichier": prop2["fichier"],
                "action": prop2["action"],
                "source_hash": prop2["source_hash"],
                "acceptes": prop2["hunks"],
            }],
        })
        res2 = json.loads(texte)["resultats"][0]
        assert res2["statut"] == "ok", res2
        print("[OK] apply après re-proposition alignée ->", res2["statut"])

        # 7) restaure l'état canonique de la démo
        _, _ = _req("/api/projects/demo/file", "PUT",
                    {"chemin": "main.py", "contenu": CANONIQUE})
        print("[OK] démo restaurée\n\nE2E STALE OK")
        return 0
    finally:
        serveur.terminate()
        try:
            serveur.wait(timeout=5)
        except subprocess.TimeoutExpired:
            serveur.kill()


if __name__ == "__main__":
    sys.exit(main())