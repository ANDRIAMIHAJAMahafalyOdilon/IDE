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
import time
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_tache as at  # noqa: E402
from app.services import opencode as oc  # noqa: E402


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


class FauxStdoutMuet:
    """readline() ne rend JAMAIS la main — l'agent est parti sur un `npm run dev`.

    Dors très longtemps plutôt que pour l'éternité : le test doit se terminer
    même si la borne de temps dupliquée ici se dégrade.
    """

    def __init__(self, duree: float = 30.0):
        self._duree = duree
        self._vide = False

    def readline(self) -> str:
        time.sleep(self._duree)
        self._vide = True
        return ""


class FauxProcessusMuet(FauxProcessus):
    def __init__(self, duree: float = 30.0):
        self.stdout = FauxStdoutMuet(duree)
        self.returncode = 0
        self._tue = False

    def poll(self):
        return self.returncode if self.stdout._vide else None


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


def test_mapper_cli_attribue_un_identifiant_par_etape():
    """Chaque étape doit avoir SON id, sinon le front les fusionne en une carte.

    Le front déduplique la timeline par `id`. Avec un id unique et réutilisé
    (`step:...:current`), les N étapes d'une tâche passent par le même id et
    n'en forment qu'une : la carte affichée était la dernière, et l'historique
    de la progression disparaissait. On vérifie donc l'unicité ET que l'étape
    se ferme sur son propre `step_finish`.
    """
    textes: dict[str, str] = {}
    activites: dict[str, object] = {}

    def _etape(demarrage: bool, indice: int):
        return at.mapper_ligne(
            _evt(
                "step_start" if demarrage else "step_finish",
                sid="sess-cli",
                part={"id": f"st{indice}", "type": "step-start"},
            ),
            textes,
            racine=_racine(),
            activites=activites,
        )

    ids = []
    fermees = []
    # Le flux réel ALTERNE step_start / step_finish : chaque étape est ouverte
    # puis refermée avant la suivante. On reproduit ce rythme, sinon on teste
    # une imbrication d'étapes qu'OpenCode n'émet jamais.
    for i in range(3):
        ids.append(_etape(True, i)[0][1]["id"])
        fermees.append(_etape(False, i)[0][1]["id"])
    assert len(set(ids)) == 3, f"les étapes partagent un id : {ids}"

    # L'étape ouverte est refermée par le step_finish suivant, sur son propre id.
    assert fermees == ids, (fermees, ids)


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
    # « Je mets à jour. » précède l'écriture : c'est une annonce, elle attend
    # l'événement suivant pour être tranchée. L'outil passe, donc elle est
    # écartée ; le texte suivant est un résultat, il part d'un seul bloc.
    assert noms == ["debut", "activite", "activite", "texte", "activite", "fin"]
    debut = evts[0][1]
    assert debut["moteur"] == "opencode" and debut["mode"] == "auto"
    assert debut["session"].startswith("sess-")
    assert evts[-1][1]["session"].startswith("sess-")
    outil = evts[2][1]
    assert outil["type"] == "file_create" and outil["fichier"] == "a.py"  # relativisé
    assemble = "".join(d["delta"] for n, d in evts if n == "texte")
    assert assemble == "Je mets à jour. C'est fait.", assemble


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


def test_erreur_fournisseur_est_remontee_a_lutilisateur():
    """`opencode run` remonte l'échec du fournisseur en JSON `type: error`.

    Sans ce cas la ligne était ignorée et l'utilisateur ne voyait que
    « l'agent s'est arrêté avec le code 1 », sans cause exploitable.
    """
    lignes = [
        json.dumps({
            "type": "error",
            "error": {
                "name": "APIError",
                "data": {
                    "message": "You exceeded your current quota, please check your plan.",
                    "statusCode": 429,
                },
            },
        }),
    ]
    evts = at.mapper_ligne(lignes[0], {}, activites={})
    assert [n for n, _ in evts] == ["erreur"]
    assert evts[0][1]["code"] == "quota"

    lignes[0] = json.dumps({
        "type": "error",
        "error": {
            "name": "APIError",
            "data": {
                "message": "This model models/gemini-2.5-flash is no longer available.",
                "statusCode": 404,
            },
        },
    })
    evts = at.mapper_ligne(lignes[0], {}, activites={})
    assert evts[0][1]["code"] == "moteur_indisponible"


def test_palier_gratuit_classe_avant_le_403_auth():
    """Un 403 « free tier » n'est pas une clé refusée.

    La clé est acceptée : c'est le palier gratuit qui interdit le mode ligne de
    commande. Classé en `auth`, l'utilisateur croyait sa clé rejetée et la
    corrigeait à tort.
    """
    ligne = json.dumps({
        "type": "error",
        "error": {
            "name": "APIError",
            "data": {
                "message": "Error from provider (Console): OpenCode's free tier "
                           "can only be used from within OpenCode",
                "statusCode": 403,
            },
        },
    })
    erreur = at.mapper_ligne(ligne, {}, activites={})[0][1]
    assert erreur["code"] == "palier_gratuit", erreur
    assert "compte payant" in erreur["message"]


def test_echec_fournisseur_interrompt_la_tache():
    """Après une erreur fournisseur, on ne doit pas attendre le `code 1`."""
    lignes = [
        json.dumps({
            "type": "error",
            "error": {
                "name": "APIError",
                "data": {"message": "quota", "statusCode": 429},
            },
        }),
    ]
    evts = asyncio.run(_collect(None, "fais n'importe quoi", FauxProcessus(lignes, returncode=1)))
    noms = [n for n, _ in evts]
    assert noms == ["erreur"], noms
    assert evts[0][1]["code"] == "quota"
    assert not any(n == "fin" for n in noms)


def test_echec_ne_fabrique_pas_de_session_opencode():
    """Un échec immédiat ne doit produire AUCUN identifiant de session.

    L'API mémorise `debut`/`fin` pour rejouer l'identifiant au tour suivant via
    `-s`. Si l'échec_sortait « tache », l'API le stockait, et la requête
    suivante passait `-s tache` : OpenCode répond « Session not found », sort en
    code 1, et le tour suivant échoue à son tour. Un échec transitoire — un
    réseau coupé, un quota — rendait la session inutilisable pour toujours.
    """
    evts = asyncio.run(
        _collect(None, "fais n'importe quoi", FauxProcessus([], returncode=1))
    )
    debut = evts[0][1]
    assert debut["session"] is None, debut["session"]
    assert not any(n == "fin" for n, _ in evts)
    # Aucune valeur memorisable ne doit subsister.
    assert all(
        n != "fin" or not (d.get("session") or "").startswith("ses")
        for n, d in evts
    )


def test_session_reelle_est_reprise_au_tour_suivant():
    """Le cas nominal : l'identifiant lu dans le flux est bien celui de l'API."""
    sid_opencode = "ses_f12e2587effeqYuFDWT5SeekI6"
    lignes = [
        _evt("step_start", sid=sid_opencode, part={"id": "s1", "type": "step-start"}),
        _evt("text", sid=sid_opencode, part={"id": "p1", "type": "text", "text": "OK"}),
        _evt("step_finish", sid=sid_opencode, part={"id": "s2", "type": "step-finish"}),
    ]
    evts = asyncio.run(_collect(None, "vas-y", FauxProcessus(lignes)))
    debut = dict(evts[0][1])
    fin = dict(evts[-1][1])
    assert debut["session"] == sid_opencode
    assert fin["session"] == sid_opencode


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


def test_consigne_autonome_borne_le_dossier_et_proportionne_l_action():
    racine = pathlib.Path("C:/Users/Roch/mon-projet")
    consigne = at.construire_consigne_tache(racine, "lance le backend")
    assert "C:\\Users\\Roch\\mon-projet" in consigne
    assert "chemin périmé" not in consigne
    assert "ne fabrique pas de Start-Process" in consigne
    assert "<demande_utilisateur>\nlance le backend" in consigne


def test_tache_sans_processus_demarre_la_cli_et_non_le_serveur():
    """En mode `cli`, sans processus injecté, c'est bien la CLI qui démarre.

    Cette vérification protège contre le double `if processus is None` qui
    rendait auparavant tout le code CLI inatteignable. Elle cible désormais
    explicitement `moteur="cli"` : le chemin de production est le serveur, et ce
    test ne doit pas décider du comportement par défaut.
    """
    appels = {"nombre": 0}
    ancien = at._demarrer

    def demarrer(binaire, racine, message, sid):
        appels["nombre"] += 1
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
                    racine, "teste la CLI", binaire="opencode.exe", moteur="cli"
                )
            ]

        evts = asyncio.run(collect_without_injected_process())
    finally:
        at._demarrer = ancien

    assert appels["binaire"] == "opencode.exe"
    assert appels["nombre"] == 1
    assert "<demande_utilisateur>\nteste la CLI" in appels["message"]
    assert "Dossier de travail autorisé et unique" in appels["message"]
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


# ── Serveur OpenCode : system prompt et arrêt propre ────────────────────────

def test_system_prompt_est_envoye_a_lagent():
    """Le contrat « proposeur seul » ne tient que si `system` accompagne le
    message. Sans lui, OpenCode peut écrire alors que l'utilisateur n'a validé
    qu'une proposition. Protège contre la régression du champ retiré."""
    capture: dict = {}

    class FauxClient:
        def post(self, url, **kw):
            capture["url"] = url
            capture["corps"] = kw.get("json") or {}
            raise RuntimeError("stop")

    ancien = oc._client
    oc._client = lambda timeout=None: FauxClient()
    try:
        try:
            oc.envoyer_instruction("ses_1", "C:/p", "modifie app.py")
        except Exception:  # noqa: BLE001 — le faux client lève toujours
            pass
    finally:
        oc._client = ancien

    assert capture["url"].endswith("/session/ses_1/message")
    assert capture["corps"].get("system") == oc.SYSTEM_PROMPT
    assert "ne peux PAS" in oc.SYSTEM_PROMPT  # l'interdiction d'écrire est explicite


def test_arreter_serveur_ne_tue_pas_un_serveur_externe():
    """Un `opencode serve` lancé par l'utilisateur ne doit jamais être tué."""
    oc._SERVEUR_PROCESS = None
    oc.arreter_serveur()  # ne doit pas lever


def test_arreter_serveur_arrete_le_processus_lance_par_le_backend():
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    oc._SERVEUR_PROCESS = proc
    try:
        oc.arreter_serveur()
        assert oc._SERVEUR_PROCESS is None
        assert proc.poll() is not None  # bien arrêté, pas laissé orphelin
    finally:
        if proc.poll() is None:
            proc.kill()
        oc._SERVEUR_PROCESS = None


def test_mapper_serveur_ne_reaffiche_pas_le_texte():
    """Le texte ne doit apparaître qu'une fois.

    OpenCode diffuse le texte d'une part en deltas puis renvoie l'instantané
    complet de la même part. Les deux gestionnaires émettent des événements
    `texte` : sans état partagé, la réponse complète était renvoyée une seconde
    fois et s'affichait en triple dans le panneau.
    """
    racine = _racine()
    outils: dict = {}

    at.mapper_evenement_serveur({
        "type": "message.updated",
        "properties": {"info": {"id": "msg-1", "role": "assistant", "modelID": "m"}},
    }, racine, outils)

    deltas = [
        {"type": "message.part.delta", "properties": {
            "messageID": "msg-1", "partID": "prt-1", "field": "text", "delta": d}}
        for d in ("Fusionne ", "Session + Type ", "en une colonne.")
    ]
    snapshot = {
        "type": "message.part.updated",
        "properties": {"part": {
            "id": "prt-1", "messageID": "msg-1", "type": "text",
            "text": "Fusionne Session + Type en une colonne.",
        }},
    }

    sortie = []
    for evt in [*deltas, snapshot]:
        sortie += at.mapper_evenement_serveur(evt, racine, outils)

    texte = "".join(d["delta"] for nom, d in sortie if nom == "texte")
    assert texte == "Fusionne Session + Type en une colonne.", texte
    assert len([nom for nom, _ in sortie if nom == "texte"]) == 3


def test_mapper_serveur_texte_reecrit_ne_double_pas():
    """Un texte réécrit (et non allongé) ne doit pas être renvoyé en entier."""
    racine = _racine()
    outils: dict = {}
    at.mapper_evenement_serveur({
        "type": "message.updated",
        "properties": {"info": {"id": "msg-2", "role": "assistant", "modelID": "m"}},
    }, racine, outils)

    sortie = at.mapper_evenement_serveur({
        "type": "message.part.delta",
        "properties": {"messageID": "msg-2", "partID": "p", "field": "text",
                       "delta": "bonne reponse"},
    }, racine, outils)
    sortie += at.mapper_evenement_serveur({
        "type": "message.part.updated",
        "properties": {"part": {"id": "p", "messageID": "msg-2", "type": "text",
                                "text": "autre chose"}},
    }, racine, outils)

    assert [d for nom, d in sortie if nom == "texte"] == [{"delta": "bonne reponse"}]


def test_serveur_recoit_la_config_de_lapplication():
    """Le serveur doit démarrer avec OPENCODE_CONFIG pointant sur notre config.

    Son `cwd` est le projet de l'utilisateur, donc OpenCode y découvre la config
    de CE projet et ignore la nôtre. Sans cette variable, l'agent tourne avec les
    valeurs par défaut (tout autorisé, aucune consigne) et il explore au lieu
    d'agir.
    """
    racine = _racine()
    captures: dict = {}

    class FauxProcessus:
        def poll(self):
            return None

    def faux_popen(*args, **kwargs):
        captures["env"] = kwargs.get("env") or {}
        captures["cwd"] = kwargs.get("cwd")
        return FauxProcessus()

    reel_popen = oc.subprocess.Popen
    reel_verif = oc.verifier_serveur
    reel_sante = oc._sante
    reel_bin = oc._serveur_binaire
    oc.subprocess.Popen = faux_popen
    # `_sante(base)` et non `verifier_serveur()` : depuis le serveur dédié, la
    # boucle de démarrage interroge la base qu'elle vient de démarrer. Neutraliser
    # le seul `verifier_serveur` laissait la sonde toucher le vrai port 4096, et le
    # test finissait sur `kill()` — 15 s d'attente, pas une assertion.
    oc.verifier_serveur = lambda: False
    oc._sante = lambda base: True
    oc._serveur_binaire = lambda: "opencode"
    try:
        oc.assurer_serveur(racine)
    finally:
        oc.subprocess.Popen = reel_popen
        oc.verifier_serveur = reel_verif
        oc._sante = reel_sante
        oc._serveur_binaire = reel_bin

    assert captures["cwd"] == str(racine.resolve())
    assert "OPENCODE_CONFIG" in captures["env"], captures["env"].keys()
    assert Path(captures["env"]["OPENCODE_CONFIG"]).name == "opencode.jsonc"
    assert Path(captures["env"]["OPENCODE_CONFIG"]).is_file()


def test_mode_cli_transmet_la_config_de_lapplication():
    """Le mode réellement utilisé par l'API est `opencode run` (--auto), pas le serveur.

    Il part lui aussi avec `--dir` sur le projet de l'utilisateur, donc sans
    OPENCODE_CONFIG il ignore nos consignes de permissions et l'agent explore au
    lieu d'agir. Ce test vise `_demarrer`, pas `assurer_serveur` : c'est le seul
    chemin que l'API emprunte.
    """
    captures: dict = {}

    class FauxProcessus:
        def poll(self):
            return None

    def faux_popen(*args, **kwargs):
        captures["env"] = kwargs.get("env") or {}
        captures["cwd"] = kwargs.get("cwd")
        return FauxProcessus()

    reel_popen = at.subprocess.Popen
    reel_bin = at.resoudre_binaire
    at.subprocess.Popen = faux_popen
    at.resoudre_binaire = lambda: "opencode"
    try:
        at._demarrer("opencode", _racine(), "fais quelque chose", None)
    finally:
        at.subprocess.Popen = reel_popen
        at.resoudre_binaire = reel_bin

    assert "OPENCODE_CONFIG" in captures["env"], captures["env"].keys()
    assert Path(captures["env"]["OPENCODE_CONFIG"]).name == "opencode.jsonc"
    assert Path(captures["env"]["OPENCODE_CONFIG"]).is_file()


def test_annonce_precedant_un_outil_est_ecartee():
    """« I'll check the file first. » puis un outil : c'est du raisonnement."""
    textes, activites = {}, {}
    sid = "sess-abc123def456"
    assert at.mapper_ligne(
        _evt("step_start", sid=sid, part={"id": "s1", "type": "step-start"}), textes,
        activites=activites,
    )
    evts = at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p1", "type": "text", "text": "I'll check the file first."}),
        textes, activites=activites,
    )
    assert [n for n, _ in evts] == [], "le texte ne doit pas partir avant le verdict"
    evts = at.mapper_ligne(
        _evt("tool_use", sid=sid, part={"id": "t1", "type": "tool", "tool": "read",
              "state": {"input": {"filePath": "app.py"}, "status": "running"}}),
        textes, activites=activites,
    )
    assert [n for n, _ in evts] == ["activite"]
    evts = at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p2", "type": "text", "text": "Ajouté dans app.py:5."}),
        textes, activites=activites,
    )
    assert [d["delta"] for n, d in evts if n == "texte"] == ["Ajouté dans app.py:5."]


def test_reponse_unique_en_debut_etape_part_quand_meme():
    """Pas d'outil derrière : le texte est la réponse, il part à la fin de l'étape.

    C'est le cas le plus fréquent (l'agent a fini, il résume). Il ne doit surtout
    pas être perdu, et il ne doit pas être retardé jusqu'à l'événement suivant.
    """
    textes, activites = {}, {}
    sid = "sess-abc123def456"
    at.mapper_ligne(_evt("step_start", sid=sid, part={"id": "s1", "type": "step-start"}), textes,
                    activites=activites)
    evts = at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p1", "type": "text", "text": "D'abord, le bug est ligne 12."}),
        textes, activites=activites,
    )
    assert [n for n, _ in evts] == []
    evts = at.mapper_ligne(
        _evt("step_finish", sid=sid, part={"id": "s2", "type": "step-finish"}), textes,
        activites=activites,
    )
    assert [d["delta"] for n, d in evts if n == "texte"] == ["D'abord, le bug est ligne 12."]
    assert any(n == "activite" for n, _ in evts)


def test_narration_ne_fuit_pas_au_reemargement():
    """Le part de l'annonce est purgé : un snapshot plus long repart de zéro.

    Sans ça, le réémargement du même part renverrait la queue de l'annonce —
    exactement le fragment « … first. » vu dans l'interface.
    """
    textes, activites = {}, {}
    sid = "sess-abc123def456"
    at.mapper_ligne(_evt("step_start", sid=sid, part={"id": "s1", "type": "step-start"}), textes,
                    activites=activites)
    at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p1", "type": "text", "text": "I'll check."}),
        textes, activites=activites,
    )
    at.mapper_ligne(
        _evt("tool_use", sid=sid, part={"id": "t1", "type": "tool", "tool": "read",
              "state": {"input": {"filePath": "app.py"}, "status": "running"}}),
        textes, activites=activites,
    )
    evts = at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p1", "type": "text", "text": "Je regarde app.py."}),
        textes, activites=activites,
    )
    assert [d["delta"] for n, d in evts if n == "texte"] == ["Je regarde app.py."]


def test_annonce_non_traitee_par_le_mauvais_etape():
    """Une attente laissée par l'étape précédente ne doit pas contaminer la suivante."""
    textes, activites = {}, {}
    sid = "sess-abc123def456"
    at.mapper_ligne(_evt("step_start", sid=sid, part={"id": "s1", "type": "step-start"}), textes,
                    activites=activites)
    at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p1", "type": "text", "text": "annonce oubliee"}),
        textes, activites=activites,
    )
    at.mapper_ligne(_evt("step_finish", sid=sid, part={"id": "s2", "type": "step-finish"}), textes,
                    activites=activites)
    at.mapper_ligne(_evt("step_start", sid=sid, part={"id": "s3", "type": "step-start"}), textes,
                    activites=activites)
    evts = at.mapper_ligne(
        _evt("text", sid=sid, part={"id": "p9", "type": "text", "text": "reponse propre"}),
        textes, activites=activites,
    )
    assert evts == []  # nouvelle attente, aucun reliquat
    evts = at.mapper_ligne(
        _evt("step_finish", sid=sid, part={"id": "s4", "type": "step-finish"}), textes,
        activites=activites,
    )
    assert [d["delta"] for n, d in evts if n == "texte"] == ["reponse propre"]


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


def test_tache_muette_expire_au_delai_et_tue_l_arbre():
    """Un agent lancé sur un `npm run dev` ne produit PLUS JAMAIS de ligne.

    Régression : le délai n'était revérifié qu'AVANT chaque `readline`, et
    `asyncio.to_thread(readline)` bloque sans borne. Une commande qui ne rend
    jamais la main neutralisait donc le timeout et la tâche pendait
    indéfiniment — reproduit sur un vrai projet, avec 4 arbres `npm run dev`
    laissés orphelins derrière. Ici, un flux qui dort plus longtemps que le
    budget doit faire échouer la tâche en une fraction de seconde.
    """
    racine = _racine()
    evts: list = []

    async def _collecter():
        async for nom, data in at.executer_tache(
            racine, "lance npm run dev",
            processus=FauxProcessusMuet(),
            timeout=0.6,
        ):
            evts.append((nom, data))

    depart = time.monotonic()
    asyncio.run(_collecter())
    duree = time.monotonic() - depart

    erreurs = [d for nom, d in evts if nom == "erreur"]
    assert erreurs, f"aucune erreur émise : {evts}"
    assert erreurs[0]["code"] == "timeout", erreurs[0]
    assert duree < 4, f"la tâche a mis {duree:.1f} s à expirer (bloquée ?)"
    assert any("arrière-plan" in (d.get("message") or "") for _, d in evts), evts


def test_tache_muette_ne_laisse_pas_le_processus_dans_le_registre():
    """Le processus doit disparaître du registre, sinon l'annulation vise un mort."""
    racine = _racine()

    async def _collecter():
        async for _ in at.executer_tache(
            racine, "rien", processus=FauxProcessus([]), timeout=0.5,
        ):
            pass

    asyncio.run(_collecter())
    assert str(racine) not in at._PROCESSUS, at._PROCESSUS


def test_activite_analyse_est_fermee_quand_le_statut_quitte_busy():
    """« Analyse OpenCode » doit passer de « En cours » à un état final.

    L'activité porte un id CONSTANT, censé être remplacé sur place. Tant que le
    statut reste `busy`, c'est normal ; dès qu'il change, un événement de clôture
    doit être émis, sinon l'interface affiche « ● En cours… » jusqu'à la fin de la
    session alors que l'agent a terminé depuis longtemps.
    """
    racine = Path(tempfile.gettempdir())
    outils: dict[str, object] = {}

    def statut(valeur: str):
        return at.mapper_evenement_serveur(
            {
                "type": "session.status",
                "properties": {"sessionID": "sess-analyse", "status": valeur},
            },
            racine,
            outils,
        )

    busy = statut("busy")
    assert [genre for genre, _ in busy] == ["etat", "activite"]
    assert busy[1][1]["status"] == "running"

    fini = statut("idle")
    activites = [charge for genre, charge in fini if genre == "activite"]
    assert activites, "le changement de statut doit clôturer l'activité"
    assert activites[0]["id"] == busy[1][1]["id"], "même id : le frontend remplace"
    assert activites[0]["status"] == "success", "plus « En cours » pour toute la session"


def test_fermeture_ne_depend_pas_du_nom_du_statut():
    """OpenCode n'annonce pas toujours « idle » : tout ce qui quitte `busy` doit
    clôturer l'activité, sinon un statut inconnu la laisserait « En cours »."""
    racine = Path(tempfile.gettempdir())
    for valeur in ("idle", "done", "completed", "error"):
        sorties = at.mapper_evenement_serveur(
            {
                "type": "session.status",
                "properties": {"sessionID": "sess-x", "status": valeur},
            },
            racine,
            {},
        )
        activites = [charge for genre, charge in sorties if genre == "activite"]
        assert activites, f"{valeur} doit clôturer l'activité"
        assert activites[0]["status"] != "running"


if __name__ == "__main__":
    sys.exit(_tout_executer())
