"""Tests du mode autonome : mapper d'événements + générateur exécuter_tache.

Exécutables sans réseau ni moteur :
     python tests/test_tache.py
Le processus réel (opencode.exe) est simulé par un faux processus injecté.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_tache as at  # noqa: E402


class FauxStdout:
    """Flux ligne-à-ligne : readline() bloque puis retourne les lignes fournies."""

    def __init__(self, lignes: list[str]):
        self._lignes = iter(lignes)
        self._vide = False

    def readline(self) -> str:
        try:
            return next(self._lignes)
        except StopIteration:
            self._vide = True
            return ""


class FauxProcessus:
    def __init__(self, lignes: list[str], returncode: int = 0):
        self.stdout = FauxStdout(lignes)
        self.returncode = returncode
        self._tue = False

    def poll(self):
        return self.returncode if self.stdout._vide else None

    def wait(self, timeout: float = 5) -> int:
        return self.returncode

    def kill(self):
        self._tue = True


def _evt(genre: str, part: dict | None = None, sid: str | None = None) -> str:
    """Événement au format réel opencode ≥1.18 : `type`/`part`/`sessionID` au niveau racine."""
    evt: dict = {"type": genre}
    if part is not None:
        evt["part"] = part
    if sid:
        evt["sessionID"] = sid
    return json.dumps(evt, ensure_ascii=False)


def _racine() -> pathlib.Path:
    racine = pathlib.Path(tempfile.gettempdir()) / "tache-test-unitaire"
    racine.mkdir(exist_ok=True)
    return racine


# ────────────────────────────── mapper_ligne ───────────────────────────────

def test_mapper_texte_accumule_delta():
    textes: dict[str, str] = {}
    l1 = _evt("text", part={"id": "p1", "type": "text", "text": "Bonj"})
    l2 = _evt("text", part={"id": "p1", "type": "text", "text": "Bonjour le monde"})
    evts1 = at.mapper_ligne(l1, textes)
    evts2 = at.mapper_ligne(l2, textes)
    assert evts1 == [("texte", {"delta": "Bonj"})]
    assert evts2 == [("texte", {"delta": "our le monde"})]


def test_mapper_cli_affiche_etapes_et_outils():
    textes: dict[str, str] = {}
    activites: dict[str, object] = {}
    racine = _racine()
    debut = at.mapper_ligne(
        _evt("step_start", sid="sess-cli", part={"id": "s1", "type": "step-start"}),
        textes,
        racine=racine,
        activites=activites,
    )
    outil = at.mapper_ligne(
        _evt(
            "tool_use",
            sid="sess-cli",
            part={
                "id": "call-1",
                "type": "tool",
                "tool": "bash",
                "state": {
                    "input": {"command": "pytest -q"},
                    "output": "12 passed",
                    "status": "completed",
                },
            },
        ),
        textes,
        racine=racine,
        activites=activites,
    )
    fin = at.mapper_ligne(
        _evt("step_finish", sid="sess-cli", part={"id": "s1", "type": "step-finish"}),
        textes,
        racine=racine,
        activites=activites,
    )
    assert debut[0][0] == "activite"
    assert debut[0][1]["title"] == "Analyse du projet"
    assert outil[0][0] == "activite"
    assert outil[0][1]["type"] == "terminal"
    assert outil[0][1]["commande"] == "pytest -q"
    assert outil[0][1]["output"] == "12 passed"
    assert fin[0][1]["status"] == "success"


def test_mapper_cli_affiche_le_diff_de_modification():
    textes: dict[str, str] = {}
    racine = _racine()
    events = at.mapper_ligne(
        _evt("tool_use", part={
            "id": "edit-1",
            "type": "tool",
            "tool": "edit",
            "state": {"input": {
                "filePath": str(racine / "main.py"),
                "oldString": "return 1",
                "newString": "return 2",
            }},
        }),
        textes,
        racine=racine,
    )
    info = events[0][1]
    assert info["type"] == "file_write"
    assert "- return 1" in info["diff"]
    assert "+ return 2" in info["diff"]


def test_mapper_outil_write_et_bash_relativise_les_chemins():
    textes = {}
    racine = _racine()
    fichier_absolu = (racine / "src" / "main.py")
    evts_edit = at.mapper_ligne(
        _evt(
            "tool_use",
            part={
                "id": "t1",
                "type": "tool",
                "tool": "write",
                "state": {
                    "input": {"filePath": str(fichier_absolu), "content": "b"},
                    "output": "Wrote file successfully.",
                    "status": "completed",
                },
            },
        ),
        textes,
        racine=racine,
    )
    assert evts_edit[0][0] == "activite"
    info = evts_edit[0][1]
    assert info["type"] == "file_create"
    assert info["fichier"] == "src/main.py"  # relatif au projet
    assert info["resume"] == "Wrote file successfully."

    evts_bash = at.mapper_ligne(
        _evt(
            "tool_use",
            part={
                "id": "t2",
                "type": "tool",
                "tool": "bash",
                "state": {
                    "input": {"command": "python -m pytest"},
                    "last_output": "3 passed",
                    "status": "completed",
                },
            },
        ),
        textes,
        racine=racine,
    )
    info_bash = evts_bash[0][1]
    assert info_bash["type"] == "terminal"
    assert info_bash["commande"] == "python -m pytest"
    assert info_bash["resume"] == "3 passed"


def test_mapper_garde_le_chemin_absolu_hors_projet():
    textes = {}
    racine = _racine()
    hors = pathlib.Path(tempfile.gettempdir()) / "autre-pile" / "x.py"
    evts = at.mapper_ligne(
        _evt("tool_use", part={
            "id": "t",
            "type": "tool",
            "tool": "read",
            "state": {"input": {"filePath": str(hors)}},
        }),
        textes,
        racine=racine,
    )
    assert evts[0][1]["fichier"] == str(hors)


def test_mapper_ignore_prose_et_autres_events():
    textes = {}
    assert at.mapper_ligne("ligne pas json", textes) == []
    assert at.mapper_ligne("", textes) == []
    assert at.mapper_ligne(_evt("step_start", part={"type": "step-start"}), textes)
    assert at.mapper_ligne(_evt("step_finish", part={"type": "step-finish"}), textes)
    assert at.mapper_ligne(_evt("message", part={"role": "user", "type": "message"}), textes) == []


# ───────────────────────────── exécuter_tache ──────────────────────────────

async def _collect(binaire, message, processus, sid=None, timeout=30):
    racine = pathlib.Path(tempfile.gettempdir()) / "tache-test-unitaire"
    racine.mkdir(exist_ok=True)
    evts = []
    async for nom, data in at.executer_tache(
        racine, message, sid_opencode=sid, binaire=binaire, processus=processus,
        timeout=timeout,
    ):
        evts.append((nom, data))
    return evts


def test_mapper_evenements_serveur_outils_et_permission():
    racine = _racine()
    outils = {}
    called = {
        "id": "evt-tool",
        "type": "session.next.tool.called",
        "properties": {
            "sessionID": "sess-server",
            "callID": "call-1",
            "tool": "bash",
            "input": {"command": "npm run build"},
        },
    }
    events = at.mapper_evenement_serveur(called, racine, outils)
    assert events[0][0] == "activite"
    assert events[0][1]["type"] == "terminal"
    assert events[0][1]["commande"] == "npm run build"

    permission = {
        "id": "evt-permission",
        "type": "permission.asked",
        "properties": {
            "id": "per-1",
            "sessionID": "sess-server",
            "permission": "bash",
            "patterns": ["npm run build"],
        },
    }
    p_events = at.mapper_evenement_serveur(permission, racine, outils)
    assert p_events == [("permission", {
        "id": "per-1", "status": "waiting_for_permission",
        "action": "bash", "resources": ["npm run build"],
        "session": "sess-server",
    })]


def test_tache_succes_flux_complet():
    racine = _racine()
    target = racine / "a.py"
    lignes = [
        _evt("step_start", sid="sess-abc123def456", part={"id": "s1", "type": "step-start"}),
        _evt("text", sid="sess-abc123def456", part={"id": "p1", "type": "text", "text": "Je mets à jour."}),
        _evt(
            "tool_use",
            sid="sess-abc123def456",
            part={"id": "t1", "type": "tool", "tool": "write",
                  "state": {"input": {"filePath": str(target)}, "status": "completed"}},
        ),
        _evt("text", sid="sess-abc123def456", part={"id": "p1", "type": "text", "text": "Je mets à jour. C'est fait."}),
        _evt("step_finish", sid="sess-abc123def456", part={"id": "s2", "type": "step-finish"}),
    ]
    evts = asyncio.run(_collect(None, "corrige a.py", FauxProcessus(lignes)))
    noms = [n for n, _ in evts]
    assert noms == ["debut", "activite", "texte", "activite", "texte", "activite", "fin"]
    debut = evts[0][1]
    assert debut["moteur"] == "opencode" and debut["mode"] == "auto"
    assert debut["session"].startswith("sess-")
    assert evts[-1][1]["session"].startswith("sess-")
    outil = evts[3][1]
    assert outil["type"] == "file_create" and outil["fichier"] == "a.py"  # relativisé


def test_tache_conserve_plusieurs_outils_dans_une_tache():
    racine = _racine()
    lignes = [
        _evt("step_start", sid="sess-multi", part={"type": "step-start"}),
        _evt("tool_use", sid="sess-multi", part={
            "id": "r", "type": "tool", "tool": "read",
            "state": {"input": {"filePath": str(racine / "a.py")}},
        }),
        _evt("tool_use", sid="sess-multi", part={
            "id": "w", "type": "tool", "tool": "write",
            "state": {"input": {"filePath": str(racine / "a.py")}},
        }),
        _evt("tool_use", sid="sess-multi", part={
            "id": "b", "type": "tool", "tool": "bash",
            "state": {"input": {"command": "python -m pytest"}},
        }),
    ]
    evts = asyncio.run(_collect(None, "corrige puis teste", FauxProcessus(lignes)))
    outils = [data["type"] for nom, data in evts if nom == "activite"]
    assert outils == ["thinking", "file_read", "file_create", "terminal"]


def test_tache_echoue_sans_evenements():
    evts = asyncio.run(
        _collect(None, "fais n'importe quoi", FauxProcessus([], returncode=1))
    )
    assert evts[0][0] == "debut"
    assert evts[1][0] == "erreur"
    assert evts[1][1]["code"] == "interne"
    assert "code 1" in evts[1][1]["message"]


def test_tache_consigne_vide():
    try:
        asyncio.run(_collect(None, "   ", FauxProcessus([], returncode=0), timeout=2))
    except at.ErreurTache as exc:
        assert exc.code == "schema_invalide"
    else:
        raise AssertionError("ErreurTache attendue pour une consigne vide")


def test_commande_reprend_session_sans_fork():
    cmd = at._commande("opencode.exe", pathlib.Path("C:/p"), "consigne", "sess-111")
    assert cmd[:6] == ["opencode.exe", "run", "--format", "json", "--auto", "--dir"]
    assert "-s" in cmd and "sess-111" in cmd and "--fork" not in cmd
    assert "--model" in cmd and at.OPENCODE_MODEL in cmd
    assert cmd[-1] == "consigne"


def test_tache_sans_processus_demarre_la_cli_et_non_le_serveur():
    """Le chemin de production doit lancer `opencode run` par défaut.

    Cette vérification protège contre le double `if processus is None` qui
    rendait auparavant tout le code CLI inatteignable.
    """
    appels = {}
    ancien = at._demarrer

    def demarrer(binaire, racine, message, sid):
        appels.update(binaire=binaire, racine=racine, message=message, sid=sid)
        return FauxProcessus([
            _evt("text", sid="sess-cli", part={"id": "p", "type": "text", "text": "OK"}),
        ])

    at._demarrer = demarrer
    try:
        async def collect_without_injected_process():
            racine = _racine()
            return [
                evt
                async for evt in at.executer_tache(
                    racine, "teste la CLI", binaire="opencode.exe"
                )
            ]

        evts = asyncio.run(collect_without_injected_process())
    finally:
        at._demarrer = ancien

    assert appels["binaire"] == "opencode.exe"
    assert appels["message"] == "teste la CLI"
    assert [nom for nom, _ in evts] == ["debut", "texte", "fin"]


def test_binaire_invalide_environ():
    ancien = at.OPENCODE_BIN
    at.OPENCODE_BIN = "C:/n'existe/opencode.exe"
    try:
        try:
            at.resoudre_binaire()
        except at.ErreurTache as exc:
            assert exc.code == "moteur_indisponible"
        else:
            raise AssertionError("ErreurTache attendue pour un binaire absent")
    finally:
        at.OPENCODE_BIN = ancien


def _tout_executer():
    tests = sorted(
        (nom, obj) for nom, obj in globals().items()
        if nom.startswith("test_") and callable(obj)
    )
    nb_ok = 0
    for nom, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  [E] {nom} : {exc!r}")
        else:
            nb_ok += 1
            print(f"  [OK] {nom}")
    print(f"\n{nb_ok}/{len(tests)} tests réussis")
    return 0 if nb_ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_tout_executer())
