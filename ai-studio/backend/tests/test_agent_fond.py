"""Tests du forçage en arrière-plan des commandes serveur (`agent_fond.py`).

    python tests/test_agent_fond.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_fond as af  # noqa: E402

# Doivent être mises en arrière-plan.
A_FONDRE = [
    "npm run dev",
    "npm run start",
    "npm run watch",
    "npm run serve",
    "npm run preview",
    "yarn dev",
    "pnpm start",
    "npx next dev",
    "next dev",
    "vite",
    "nodemon server.js",
    "tsx watch src/server.ts",
    "flask run",
    "python -m flask run --port 5000",
    "python manage.py runserver",
    "uvicorn app:main --reload",
    "python -m http.server 8000",
    "cd backend && npm run dev",
]

# Doivent rester au premier plan : un faux positif casserait l'agent.
AU_PREMIER_PLAN = [
    "npm test",
    "npm install",
    "npm ci",
    "npm run build",
    "npm run lint",
    "git status",
    "git log --oneline",
    "pip install -r requirements.txt",
    "python main.py",
    "pytest -q",
    "npx tsc --noEmit",
    "npm run prisma:generate",
    "vite build",
    "next build",
]


def test_les_commandes_serveur_sont_detectees():
    echecs = []
    for commande in A_FONDRE:
        if af.commande_bloquante(commande) is None:
            echecs.append(commande)
    assert not echecs, f"non détectées comme longues : {echecs}"


def test_les_commandes_courtes_ne_sont_touchees():
    faux = []
    for commande in AU_PREMIER_PLAN:
        if af.commande_bloquante(commande) is not None:
            faux.append(commande)
    assert not faux, f"faux positifs : {faux}"


def test_les_motifs_courts_priment_sur_les_motifs_longs():
    # `npm run build` contient « run » : le motif court doit gagner, sinon le
    # build passe en arrière-plan et l'agent continue avant qu'il ait fini.
    assert af.commande_bloquante("npm run build") is None
    assert af.commande_bloquante("npm run dev") is not None


def test_le_plugin_ne_reagit_qu_aux_commandes_longues():
    js = af.plugin_js()
    assert "tool.execute.before" in js
    assert "input.tool !== \"bash\"" in js
    # Sans cette garde, la session interactive de l'utilisateur verrait ses
    # serveurs partir en arrière-plan — l'inverse de ce qu'il veut.
    assert "AISTUDIO_AGENT" in js
    assert "AISTUDIO_FOND" in js
    assert "Buffer.from" in js
    assert "lancer_arriere_plan\\.cmd" in js
    assert "CommandeBase64" in af._LANCEUR
    assert "FromBase64String" in af._DETACHEUR


def test_le_plugin_embarque_tous_les_motifs():
    import json

    js = af.plugin_js()
    debut = js.index("const MOTIFS = ") + len("const MOTIFS = ")
    fin = js.index(";", debut)
    motifs = json.loads(js[debut:fin])
    assert tuple(motifs) == af.COMMANDES_LONGUES


def test_le_plugin_est_ecrit_une_seule_fois():
    cible = af.dossier_plugins() / "aistudio-arriere-plan.js"
    essai = af.dossier_plugins() / "aistudio-test-ecriture.js"
    if cible.is_file():
        import shutil

        sauvegarde = cible.with_suffix(".sauvegarde")
        shutil.copy2(cible, sauvegarde)
        try:
            assert af.installer() == cible
            avant = cible.read_text(encoding="utf-8")
            assert af.installer() == cible
            assert cible.read_text(encoding="utf-8") == avant
        finally:
            shutil.move(str(sauvegarde), str(cible))
    else:
        # Premier démarrage : rien à restaurer.
        assert af.installer() == cible


if __name__ == "__main__":
    tests = [
        test_les_commandes_serveur_sont_detectees,
        test_les_commandes_courtes_ne_sont_touchees,
        test_les_motifs_courts_priment_sur_les_motifs_longs,
        test_le_plugin_ne_reagit_qu_aux_commandes_longues,
        test_le_plugin_embarque_tous_les_motifs,
        test_le_plugin_est_ecrit_une_seule_fois,
    ]
    ok = 0
    for test in tests:
        try:
            test()
            print(f"  [OK] {test.__name__}")
            ok += 1
        except AssertionError as exc:
            print(f"  [ECHEC] {test.__name__} : {exc}")
    print(f"{ok}/{len(tests)} tests réussis")
    raise SystemExit(0 if ok == len(tests) else 1)
