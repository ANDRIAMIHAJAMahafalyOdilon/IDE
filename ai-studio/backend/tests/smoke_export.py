"""Smoke test de GET /api/projects/{projet}/export — hors données réelles."""

from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import zipfile

RACINE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

TMP = tempfile.mkdtemp()
import os  # noqa: E402

os.environ["PROJETS_DIR"] = str(pathlib.Path(TMP) / "projets")
os.environ["REGISTRE_FICHIER"] = str(pathlib.Path(TMP) / "registre.json")
os.environ["ORGANIZER_DB"] = str(pathlib.Path(TMP) / "organizer.db")
os.environ["MEMOIRE_DIR"] = str(pathlib.Path(TMP) / "sessions")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main() -> int:
    c = TestClient(app)
    echecs = []

    # 1. Projet absent -> 404, et surtout AUCUN dossier créé.
    r = c.get("/api/projects/inexistant/export")
    print(f"[{'OK' if r.status_code == 404 else 'KO'}] projet absent -> {r.status_code}")
    if r.status_code != 404:
        echecs.append("projet absent devrait répondre 404")
    cree = pathlib.Path(TMP) / "projets" / "inexistant"
    if cree.exists():
        echecs.append("l'export a créé un dossier pour un projet inexistant")
        print("[KO] dossier créé à tort")
    else:
        print("[OK] aucun dossier créé")

    # 2. Import d'un zip, puis export.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("src/main.py", "print('salut')\n")
        z.writestr("README.md", "# demo\n")
        z.writestr("node_modules/paquet/index.js", "ignoré\n")
        z.writestr(".git/config", "ignoré aussi\n")
    r = c.post(
        "/api/projects/import",
        files={"fichier": ("demo.zip", buffer.getvalue(), "application/zip")},
        data={"nom": "demo"},
    )
    print(f"[{'OK' if r.status_code == 200 else 'KO'}] import -> {r.status_code} {r.json()}")
    if r.status_code != 200:
        echecs.append("import cassé")
        return 1

    r = c.get("/api/projects/demo/export")
    print(f"[{'OK' if r.status_code == 200 else 'KO'}] export -> {r.status_code}")
    if r.status_code != 200:
        echecs.append("export cassé")
        return 1
    print(f"       content-disposition : {r.headers.get('content-disposition')}")
    print(f"       content-type        : {r.headers.get('content-type')}")

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        noms = sorted(z.namelist())
        contenu = z.read("demo/src/main.py").decode()
    attendu = ["demo/README.md", "demo/src/main.py"]
    print(f"[{'OK' if noms == attendu else 'KO'}] contenu du zip : {noms}")
    if noms != attendu:
        echecs.append(f"contenu inattendu : {noms}")
    source = "print('salut')\n"
    print(f"[{'OK' if contenu == source else 'KO'}] contenu préservé ({contenu!r})")
    if contenu != source:
        echecs.append("contenu du fichier altéré")

    # 3. Le temporaire est supprimé après envoi.
    restes = list(pathlib.Path(tempfile.gettempdir()).glob("*.zip"))
    if restes:
        print(f"[KO] {len(restes)} zip(s) laissé(s) dans le temp")
        echecs.append("zip temporaire non supprimé")
    else:
        print("[OK] zip temporaire supprimé")

    # 4. L'archive exportée se réimporte telle quelle.
    buffer.seek(0)
    r2 = c.post(
        "/api/projects/import",
        files={"fichier": ("demo.zip", r.content, "application/zip")},
        data={"nom": "demo-bis"},
    )
    print(f"[{'OK' if r2.status_code == 200 and r2.json()['nb_fichiers'] == 2 else 'KO'}] "
          f"réimport -> {r2.status_code} {r2.json()}")

    print()
    print("ECHEC :", echecs if echecs else "aucun")
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
