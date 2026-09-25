"""Smoke test de l'API réelle (FastAPI monté) : health, chat SSE, apply-changes."""
import sys, json, pathlib, hashlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app.config import PROJETS_DIR
from app.services import workspace
from app.services.diff import build_diff

client = TestClient(app)

# 1) health
r = client.get("/health")
assert r.status_code == 200 and r.json()["status"] == "ok"
print("[OK] /health")

# 2) prépare un projet de démo sur disque
projet = PROJETS_DIR / "demo"
projet.mkdir(parents=True, exist_ok=True)
workspace.ecrire_fichier(projet / "main.py", "def a():\n    return 1\n\nprint('old')\n")

# 3) chat SSE avec simulation -> propositions riches en hunks
r = client.post("/api/agent/chat", json={
    "projet": "demo",
    "message": "modifie main.py",
    "simulation": [
        {"chemin": "main.py",
         "contenu": "def a():\n    return 2\n\nprint('new')\n"}
    ],
})
assert r.status_code == 200, r.text
assert "event:" in r.text and "proposition" in r.text
print("[OK] /api/agent/chat (SSE) - événements reçus:")
for ligne in r.text.splitlines():
    if ligne.startswith("event:") or ligne.startswith("data:"):
        print("   ", ligne[:110])

# 3b) mode edit sans projet -> 422 (le projet reste requis pour éditer)
r422 = client.post("/api/agent/chat", json={"mode": "edit", "message": "x"})
assert r422.status_code == 422, r422.text
print("[OK] /api/agent/chat mode=edit - sans projet -> 422")

# 4) apply-changes : applique TOUS les hunks proposés
props = []
cur = None
for ligne in r.text.splitlines():
    if ligne.startswith("event:"):
        cur = ligne[6:].strip()
    elif ligne.startswith("data:") and cur == "proposition":
        props.append(json.loads(ligne[5:]))
assert len(props) == 1, r.text
modifs = [{
    "fichier": p["fichier"], "action": p["action"],
    "source_hash": p["source_hash"], "acceptes": p["hunks"],
} for p in props]
r = client.post("/api/agent/apply-changes", json={"projet": "demo", "modifications": modifs})
assert r.status_code == 200, r.text
resultats = r.json()["resultats"]
assert all(x["statut"] == "ok" for x in resultats), resultats
contenu = workspace.lire_fichier(projet / "main.py")
assert contenu == "def a():\n    return 2\n\nprint('new')\n", repr(contenu)
print("[OK] /api/agent/apply-changes - fichier modifié, nouveau_sha=", resultats[0]["nouveau_sha"])

# 5) cohérence source_hash : un apply périmé doit échouer proprement (statut erreur par fichier)
ancien = workspace.lire_fichier(projet / "main.py")
diff2 = build_diff(ancien, "def a():\n    return 3\n\nprint('new2')\n")
workspace.ecrire_fichier(projet / "main.py", "contenu modifie entre temps\n")
r = client.post("/api/agent/apply-changes", json={
    "projet": "demo",
    "modifications": [{
        "fichier": "main.py", "action": "write",
        "source_hash": diff2["source_hash"], "acceptes": diff2["hunks"],
    }],
})
assert r.json()["resultats"][0]["statut"] == "erreur"
print("[OK] /api/agent/apply-changes - source_hash périmé -> erreur par fichier")

# 6) contrat erreur : une erreur de l'adaptateur devient un event SSE `erreur`
import asyncio
import app.services.agent_chat as agent_chat_mod
import app.services.agent_adapter as agent_adapter_mod
from app.services.agent_adapter import ErreurAdaptateur
from app.api.agent import generer_evenements
from app.models.chat import RequeteChat, FichierSimule

def _collecter(req):
    async def _r():
        evts = []
        async for e in generer_evenements(req, workspace.racine_projet(req.projet)):
            evts.append(e)
        return evts
    return asyncio.run(_r())

_old = agent_chat_mod.generer_propositions_moteur
def _explose(*a, **k):
    raise ErreurAdaptateur("parse_format", "Sortie de l'agent non reconnaissable.", fichier="main.py")
agent_chat_mod.generer_propositions_moteur = _explose
try:
    evts = _collecter(RequeteChat(projet="demo", message="propose", session="sess-x"))
finally:
    agent_chat_mod.generer_propositions_moteur = _old
erreurs = [e for e in evts if e["event"] == "erreur"]
assert erreurs, evts
import json as _json
assert _json.loads(erreurs[0]["data"])["code"] == "parse_format"
assert _json.loads(erreurs[0]["data"])["fichier"] == "main.py"
assert evts[0]["event"] == "debut"
print("[OK] /api/agent/chat - erreur adaptateur -> event erreur {code: parse_format, fichier}")

# 6b) mode chat : discussion streamée, projet OPTIONNEL, jamais de proposition.
# (Passe par generer_discussion : un seul SSE TestClient est possible par
#  process avec sse_starlette, le premier est déjà consommé à l'étape 3.)
import app.services.agent_discussion as agent_discussion_mod
from app.api.agent import generer_discussion

def _collecter_chat(req):
    async def _r():
        return [e async for e in generer_discussion(req)]
    return asyncio.run(_r())

_old_demarrer = agent_discussion_mod.demarrer_reponse
agent_discussion_mod.demarrer_reponse = lambda prompt: ("gemini", iter(["Bon", "jour"]))
try:
    evts_chat = _collecter_chat(RequeteChat(mode="chat", message="salut", session="sess-chat"))
finally:
    agent_discussion_mod.demarrer_reponse = _old_demarrer
types_chat = [e["event"] for e in evts_chat]
assert types_chat == ["debut", "texte", "texte", "fin"], types_chat
assert "proposition" not in types_chat
assert _json.loads(evts_chat[0]["data"])["moteur"] == "gemini"
print("[OK] /api/agent/chat mode=chat - debut/texte×2/fin, sans proposition, sans projet")

# 7) endpoints projets : liste, arborescence, lecture, recherche, écriture
r = client.get("/api/projects")
assert r.status_code == 200, r.text
ids = [p["id"] for p in r.json()["projets"]]
assert "demo" in ids
print("[OK] /api/projects - {}", r.json()["projets"])

r = client.get("/api/projects/demo/tree")
assert r.status_code == 200, r.text
chemins = []

def _plat(n):
    for e in n:
        if e["type"] == "fichier":
            chemins.append(e["chemin"])
        elif e["type"] == "dossier":
            _plat(e.get("enfants", []))
_plat(r.json()["racine"])
assert "main.py" in chemins
print("[OK] /api/projects/demo/tree - {} entrées racine".format(len(r.json()["racine"])))

r = client.get("/api/projects/demo/file", params={"chemin": "main.py"})
assert r.status_code == 200, r.text
assert r.json()["langue"] == "python"
print("[OK] /api/projects/demo/file - langue={}, {} octets".format(r.json()["langue"], r.json()["taille"]))

r = client.get("/api/projects/demo/file", params={"chemin": "../etc/passwd"})
assert r.status_code == 422, r.text
print("[OK] /api/projects/demo/file - path traversal -> 422")

r = client.get("/api/projects/demo/nope.txt-x", params={"chemin": "main.py"})
assert r.status_code == 404, r.text
print("[OK] /api/projects/demo/… - projet inconnu -> 404")

r = client.put("/api/projects/demo/file", json={"chemin": "brouillon.txt", "contenu": "essai smoke\n"})
assert r.status_code == 200 and len(r.json()["nouveau_sha"]) == 40, r.text
r2 = client.get("/api/projects/demo/file", params={"chemin": "brouillon.txt"})
assert r2.json()["contenu"] == "essai smoke\n"
(projet / "brouillon.txt").unlink()
print("[OK] /api/projects/demo/file PUT -> nouveau_sha, puis cleanup")

r = client.get("/api/projects/demo/search", params={"q": "token absent du demo"})
assert r.status_code == 200, r.text
print("[OK] /api/projects/demo/search - {}".format(r.json()))

# 8) moteur réel live si RUN_LIVE=1 (serveur OpenCode actif) — sinon erreur contractuelle
import os
if os.getenv("RUN_LIVE", "0") == "1":
    workspace.ecrire_fichier(projet / "main.py",
        "def additionner(a, b):\n    return a + b\n\n\nif __name__ == \"__main__\":\n    print(additionner(2, 3))\n")
    evts = _collecter(RequeteChat(
        projet="demo",
        message="Ajoute une fonction carre(x) qui renvoie x*x, et affiche carre(4) dans le main.",
        session="sess-live",
        fichiers_contexte=["main.py"],
    ))
    types = [e["event"] for e in evts]
    nb_prop = types.count("proposition")
    print(f"[LIVE] événements : {' > '.join(types)} — propositions : {nb_prop}")
    for e in evts:
        if e["event"] in ("texte", "erreur"):
            print("  ", e["event"], "=>", e["data"][:200])
    eh_oui = evts[-1]["data"]
    print(f"[LIVE] fin => {eh_oui}")
else:
    print("[LIVE] ignoré (RUN_LIVE=1 pour lancer le moteur réel)")

# 9) restaure le contenu canonique de la démo (les étapes 4-5 l'avaient altéré)
workspace.ecrire_fichier(projet / "main.py",
    "def additionner(a, b):\n    return a + b\n\n\nif __name__ == \"__main__\":\n    print(additionner(2, 3))\n")

print("\nSMOKE TEST OK")