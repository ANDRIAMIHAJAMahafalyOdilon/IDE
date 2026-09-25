"""Smoke test API du mode « dossier direct » (FastAPI monté, TestClient).

Vérifie le câblage HTTP : preview-local, open-local, arborescence/fichiers sur le
vrai disque, anti-traversal, et le blocage du mode autonome /tache (403).

Important : la validation de chemin n'est PAS neutralisée (aucun patch de
`_racines_invalides`) — le projet de test est créé hors AppData pour être
représentatif d'un vrai dossier utilisateur.

Exécution :  python tests/smoke_dossier_local.py
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

# Registre isolé AVANT l'import de l'app (lu à l'import de registre.py).
_TMP_REG = pathlib.Path(tempfile.mkdtemp())
os.environ["REGISTRE_FICHIER"] = str(_TMP_REG / "registre.json")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import registre  # noqa: E402

client = TestClient(app)

# Projet « réel » hors AppData / hors dossiers système (validation réelle).
_BASE = pathlib.Path.home() / "_smoke_dossier_local"
shutil.rmtree(_BASE, ignore_errors=True)
racine = _BASE / "mon-projet"
(racine / "src").mkdir(parents=True)
(racine / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
# node_modules IMBRIQUÉ (comme un vrai monorepo) : doit être exclu partout.
imbrique = racine / "packages" / "lib" / "node_modules" / "dep"
imbrique.mkdir(parents=True)
(imbrique / "index.js").write_text("x", encoding="utf-8")
(racine / "node_modules" / "top").mkdir(parents=True)
(racine / "node_modules" / "top" / "i.js").write_text("x", encoding="utf-8")
(racine / "dist").mkdir()
(racine / "dist" / "bundle.js").write_text("x", encoding="utf-8")
(racine / "debug.log").write_text("bruit\n", encoding="utf-8")
(racine / ".env").write_text("SECRET=ne-doit-pas-apparaitre\n", encoding="utf-8")
(racine / ".env.local").write_text("SECRET2=idem\n", encoding="utf-8")
(racine / ".env.example").write_text("API_KEY=\n", encoding="utf-8")  # modèle -> visible
# Dot-dossiers systémiques (régression lstrip) : doivent disparaître du tree.
for _d in (".git", ".next", ".pytest_cache"):
    (_d_path := racine / _d / "sous").mkdir(parents=True)
    (_d_path / "f.txt").write_text("bruit\n", encoding="utf-8")
    (racine / _d / "direct.txt").write_text("bruit\n", encoding="utf-8")
# Volontairement PAS de `.env*` dans le .gitignore : l'exclusion doit être dure.
(racine / ".gitignore").write_text("*.log\ndist/\n", encoding="utf-8")

try:
    # 1) preview-local : validation réelle + échantillon filtré, sans ouverture
    r = client.post("/api/projects/preview-local", json={"chemin": str(racine)})
    assert r.status_code == 200, r.text
    apercu = r.json()
    assert apercu["nom"] == "mon-projet"
    assert "src/app.py" in apercu["exemples"]
    assert ".env.example" in apercu["exemples"], apercu["exemples"]  # modèle visible
    assert not any("node_modules" in e for e in apercu["exemples"]), apercu["exemples"]
    assert not any(e.startswith("dist/") or e.endswith(".log") for e in apercu["exemples"])
    assert not any(e.startswith(".env") and e != ".env.example" for e in apercu["exemples"]), apercu["exemples"]
    assert not any(e.split("/")[0] in {".git", ".next", ".pytest_cache"} for e in apercu["exemples"])
    print(f"[OK] preview-local -> {apercu['nb_fichiers']} fichiers : {apercu['exemples']}")

    # 2) chemins refusés : non absolu / inexistant / fichier
    for mauvais in ("chemin/relatif", str(_TMP_REG / "fantome"), str(racine / "src" / "app.py")):
        r = client.post("/api/projects/preview-local", json={"chemin": mauvais})
        assert r.status_code == 422, (mauvais, r.status_code, r.text)
    print("[OK] preview-local -> 422 pour non-absolu / inexistant / fichier")

    # 3) open-local : enregistre le dossier réel (aucune copie)
    r = client.post("/api/projects/open-local", json={"chemin": str(racine)})
    assert r.status_code == 200, r.text
    ouvert = r.json()
    assert ouvert["origine"] == "dossier" and ouvert["id"] == "mon-projet"
    print(f"[OK] open-local -> id={ouvert['id']} chemin={ouvert['chemin']}")
    assert (racine / "src" / "app.py").exists()        # rien n'a été déplacé/copié

    # 4) la liste des projets inclut le dossier direct + son origine
    r = client.get("/api/projects")
    entree = next(p for p in r.json()["projets"] if p["id"] == "mon-projet")
    assert entree["origine"] == "dossier"
    print("[OK] /api/projects -> {'origine': 'dossier', 'chemin': ...}")

    # 5) tree / file / search agissent sur le VRAI disque (node_modules imbriqué exclu)
    r = client.get("/api/projects/mon-projet/tree")
    assert r.status_code == 200, r.text
    plats: list[str] = []

    def _plat(n):
        for e in n:
            if e["type"] == "fichier":
                plats.append(e["chemin"])
            else:
                _plat(e.get("enfants", []))

    _plat(r.json()["racine"])
    assert "src/app.py" in plats
    assert not any("node_modules" in p for p in plats), plats
    assert not any(p.startswith((".git/", ".next/", ".pytest_cache/")) for p in plats), plats
    assert ".env" not in plats and ".env.local" not in plats, plats
    print(f"[OK] tree (direct) -> {plats}")

    r = client.put("/api/projects/mon-projet/file", json={"chemin": "src/app.py", "contenu": "print('v2')\n"})
    assert r.status_code == 200, r.text
    assert (racine / "src" / "app.py").read_text(encoding="utf-8") == "print('v2')\n"
    print("[OK] PUT file -> écrit dans le vrai fichier sur disque")

    r = client.get("/api/projects/mon-projet/file", params={"chemin": "../hors.txt"})
    assert r.status_code == 422, r.text
    print("[OK] GET file -> path traversal 422 (mode direct)")

    # 6) mode autonome /tache interdit sur un dossier direct
    r = client.post("/api/agent/tache", json={"projet": "mon-projet", "message": "fais x"})
    assert r.status_code == 403, r.text
    print("[OK] /api/agent/tache -> 403 (écriture non validée interdite)")

    # 7) enregistrement réel du registre sur disque (persistance vérifiée)
    assert pathlib.Path(os.environ["REGISTRE_FICHIER"]).is_file()
    print("[OK] registre.json écrit sur disque")

    # 8) le vrai validateur accepte un dossier utilisateur hors système (non patché)
    assert registre._racines_invalides() and all(
        p != pathlib.Path(p.anchor).resolve() for p in registre._racines_invalides()
    )
    print("[OK] validation réelle : aucune racine de lecteur dans les refus")

    print("\nSMOKE DOSSIER LOCAL OK")
finally:
    shutil.rmtree(_BASE, ignore_errors=True)
    shutil.rmtree(_TMP_REG, ignore_errors=True)
