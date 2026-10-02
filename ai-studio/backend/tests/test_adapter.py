"""Tests d'agent_adapter.py — parseur cascade + validation + orchestration.

Exécutables sans réseau ni serveur OpenCode ni clés LLM :
     python tests/test_adapter.py
"""

from __future__ import annotations

import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_adapter, moteurs, opencode  # noqa: E402
from app.services.agent_adapter import ErreurAdaptateur  # noqa: E402


def test_json_strict():
    texte = '[{"action": "write", "fichier": "a.py", "contenu": "x=1\\n"}]'
    with tempfile.TemporaryDirectory() as tmp:
        props = agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
    assert len(props) == 1 and props[0]["fichier"] == "a.py"


def test_json_croisé_dans_prose_markdown():
    texte = (
        "Voici ma proposition :\n\n"
        '```json\n[{"action": "write", "fichier": "b.py", '
        '"contenu": "print(\\\"[probe]\\\")\\n"}]\n```\n'
        "J'espère que ça ira."
    )
    with tempfile.TemporaryDirectory() as tmp:
        props = agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
    assert props[0]["action"] == "write"
    assert props[0]["contenu"] == 'print("[probe]")\n'


def test_json_objet_unique():
    texte = '{"action": "delete", "fichier": "c.py"}'
    with tempfile.TemporaryDirectory() as tmp:
        props = agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
    assert props == [{"action": "delete", "fichier": "c.py"}]


def test_protocole_fichier_avec_fences():
    texte = (
        "Résumé…\n"
        "===FILE src/x.py===\n"
        "```python\n"
        "def f():\n    return 1\n"
        "```\n"
        "===END===\n"
        "===DELETE vieux.py===\n"
        "fin"
    )
    with tempfile.TemporaryDirectory() as tmp:
        props = agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
    titres = {p["fichier"]: p["action"] for p in props}
    assert titres == {"src/x.py": "write", "vieux.py": "delete"}
    ecrit = next(p for p in props if p["fichier"] == "src/x.py")
    assert ecrit["contenu"] == "def f():\n    return 1"


def test_protocole_deduplique_et_dernier_ecrit_gagne():
    texte = (
        "===FILE a.py===\n" "contenu 1\n" "===END===\n"
        "===FILE a.py===\n" "contenu 2\n" "===END===\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        props = agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
    assert len(props) == 1 and props[0]["contenu"] == "contenu 2"


def test_rien_du_tout_parse_format():
    texte = "Je pense qu'il faut modifier a.py en profondeur, ça change beaucoup."
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "parse_format"
            return
    raise AssertionError("ErreurAdaptateur parse_format attendue")


def test_schema_invalide_fichier_vide():
    texte = '[{"action": "write", "fichier": " ", "contenu": "x\\n"}]'
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "schema_invalide"
            return
    raise AssertionError("schema_invalide attendue")


def test_schema_invalide_action_inconnue():
    texte = '[{"action": "move", "fichier": "a.py"}]'
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "schema_invalide"
            return
    raise AssertionError("schema_invalide attendue")


def test_schema_invalide_contenu_manquant():
    texte = '[{"action": "write", "fichier": "a.py"}]'
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "schema_invalide"
            return
    raise AssertionError("schema_invalide attendue")


def test_hors_projet_rejetee():
    texte = '[{"action": "write", "fichier": "../evil.txt", "contenu": "x\\n"}]'
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "hors_projet"
            assert exc.fichier == "../evil.txt"
            return
    raise AssertionError("hors_projet attendue")


def test_patch_valide_est_accepte():
    texte = (
        '[{"action": "patch", "fichier": "a.py", "operations": '
        '[{"ligne": 2, "suppression": 1, "ajout": ["y=2"]}]}]'
    )
    with tempfile.TemporaryDirectory() as tmp:
        props = agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
    assert props[0]["action"] == "patch"
    assert props[0]["operations"][0]["ligne"] == 2


def test_patch_sans_operations_rejete():
    texte = '[{"action": "patch", "fichier": "a.py"}]'
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "schema_invalide"
            return
    raise AssertionError("schema_invalide attendue")


def test_patch_operation_sans_ligne_rejetee():
    texte = '[{"action": "patch", "fichier": "a.py", "operations": [{"ajout": ["z"]}]}]'
    with tempfile.TemporaryDirectory() as tmp:
        try:
            agent_adapter.parser_propositions(texte, pathlib.Path(tmp))
        except ErreurAdaptateur as exc:
            assert exc.code == "schema_invalide"
            return
    raise AssertionError("schema_invalide attendue")


def test_application_patch_remplace_et_insere():
    from app.services.workspace import appliquer_patch
    source = "un\ndeux\ntrois\nquatre"
    assert appliquer_patch(source, [{"ligne": 2, "ajout": ["DEUX"]}]) == "un\nDEUX\ntrois\nquatre"
    # suppression = 2 lignes remplacées par 3
    assert appliquer_patch(
        source, [{"ligne": 2, "suppression": 2, "ajout": ["a", "b", "c"]}]
    ) == "un\na\nb\nc\nquatre"
    # suppression seule = suppression de ligne
    assert appliquer_patch(source, [{"ligne": 1, "suppression": 1, "ajout": []}]) == "deux\ntrois\nquatre"


def test_application_patch_plusieurs_operations_equivalentes_a_un_calcul_direct():
    """Les numéros de ligne sont ceux du fichier AVANT toute modification.

    C'est la garantie qui permet d'appliquer de la fin vers le début sans
    recalcul d'offset ; si l'ordre changeait, les positions ne correspondraient
    plus au fichier d'origine.
    """
    from app.services.workspace import appliquer_patch
    lignes = [f"l{i}" for i in range(1, 11)]
    source = "\n".join(lignes)
    resultat = appliquer_patch(
        source,
        [
            {"ligne": 2, "ajout": ["L2-remplacee"]},
            {"ligne": 7, "ajout": ["L7-remplacee"]},
        ],
    ).split("\n")
    assert resultat[1] == "L2-remplacee"
    assert resultat[6] == "L7-remplacee"
    assert resultat[0] == "l1" and resultat[2] == "l3" and resultat[8] == "l9"


def test_application_patch_rejette_les_cas_dangereux():
    from app.services.workspace import PatchInvalide, appliquer_patch
    source = "un\ndeux\ntrois"
    cas = [
        [],                                                        # aucune opération
        [{"ligne": 0, "ajout": ["x"]}],                            # ligne 0
        [{"ligne": 9, "ajout": ["x"]}],                            # hors fichier
        [{"ligne": 2, "suppression": 99, "ajout": ["x"]}],         # dépassement
        [{"ligne": 2, "ajout": "x\ny"}],                           # ajout scalaire toléré
        [{"ligne": 1, "ajout": [{"pas": "une chaine"}]}],          # ajout mal typé
        [{"ligne": 1, "suppression": 2, "ajout": ["x"]},
         {"ligne": 2, "ajout": ["y"]}],                            # chevauchement
    ]
    for operations in cas:
        if operations == [{"ligne": 2, "ajout": "x\ny"}]:
            # Une chaîne est tolérée et traitée comme UNE ligne : son saut de
            # ligne reste dans le contenu de la ligne remplacée.
            assert appliquer_patch(source, operations) == "un\nx\ny\ntrois"
            continue
        try:
            appliquer_patch(source, operations)
        except PatchInvalide:
            continue
        raise AssertionError(f"PatchInvalide attendue pour {operations!r}")


def test_patch_sapplique_au_contenu_reel_et_non_a_la_proposition():
    """Le patch part du fichier SUR LE DISQUE, jamais d'un contenu renvoyé par
    l'agent : c'est ce qui garantit qu'un fichier tronqué en contexte ne soit
    pas amputé silencieusement."""
    from app.services import agent_chat
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        cible = racine / "gros.py"
        cible.write_text("\n".join(f"l{i}" for i in range(1, 201)), encoding="utf-8")
        avant = cible.read_text(encoding="utf-8")
        sorties = []

        def faux(sid, repertoire, texte):
            # Ne renvoie que la FIN du fichier : le backend doit néanmoins
            # appliquer le patch sur les 200 lignes réelles.
            sorties.append(texte)
            return '[{"action": "patch", "fichier": "gros.py", "operations": ' \
                   '[{"ligne": 200, "ajout": ["l200-remplacee"]}]}]'

        orig = opencode.envoyer_instruction
        opencode.envoyer_instruction = faux
        try:
            resultat = agent_chat.generer_propositions_moteur(
                racine, "modifie la fin", ["gros.py"], [], "sid-test",
            )
        finally:
            opencode.envoyer_instruction = orig
        propositions = resultat["propositions"]
        assert len(propositions) == 1
        hunk = propositions[0]["hunks"][0]
        affiche = "".join(l["contenu"] for l in hunk["lignes"])
        assert "l200-remplacee" in affiche
        # Le fichier n'a pas été touché : la proposition n'est qu'un affichage.
        assert cible.read_text(encoding="utf-8") == avant


def test_serveur_avec_config_perimee_est_detecte_comme_tel():
    """Un serveur d'une installation antérieure connaît `discussion` mais pas
    `proposition` : il doit être considéré comme PÉRIMÉ et renouvelé, sinon
    l'Edit retomberait sur l'agent par défaut (qui peut écrire et exécuter)."""
    import app.services.opencode as oc

    servie = {"permission": {"bash": "allow"}, "agent": {oc.CHAT_AGENT: {"permission": {}}}}
    if all(nom in servie["agent"] for nom in (oc.CHAT_AGENT, oc.EDIT_AGENT)):
        raise AssertionError("agent proposition déjà présent : test sans objet")

    orig = oc._config_du_serveur
    oc._config_du_serveur = lambda base: servie
    try:
        assert oc._tourne_avec_notre_config("http://127.0.0.1:1") is False
        servie["agent"][oc.EDIT_AGENT] = {"permission": {}}
        assert oc._tourne_avec_notre_config("http://127.0.0.1:1") is True
    finally:
        oc._config_du_serveur = orig


def test_decoupage_fichiers_limite_et_hors_projet():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        (racine / "gros.py").write_text("x" * 50000, encoding="utf-8")
        (racine / "lignes.py").write_text("\n" * 5000, encoding="utf-8")
        from app.config import (
            FICHIER_CONTEXTE_MAX_CAR,
            FICHIER_CONTEXTE_MAX_LIGNES,
            FICHIERS_CONTEXTE_MAX,
        )
        bloc = agent_adapter.decouper_bloc_fichiers(
            ["gros.py", "lignes.py", "../sans.js"], racine
        )
    assert "gros.py" in bloc
    assert "sans.js" not in bloc
    # Les lignes sont numérotées : c'est ce qui rend le patch possible.
    assert re.search(r"^\s*1 \| x", bloc, re.MULTILINE)
    # Borné PAR FICHIER, en caractères ET en lignes : la numérotation coûte
    # 8 car. par ligne, donc un fichier fait uniquement de retours à la ligne
    # ferait exploser le budget si seul le plafond de caractères existait.
    numeroes = re.findall(r"^\s*\d+ \| ", bloc, re.MULTILINE)
    assert len(numeroes) <= FICHIERS_CONTEXTE_MAX * FICHIER_CONTEXTE_MAX_LIGNES
    assert len(bloc) <= FICHIERS_CONTEXTE_MAX * (
        FICHIER_CONTEXTE_MAX_CAR + 9 * FICHIER_CONTEXTE_MAX_LIGNES
    ) + 200


# ───────────────────────── Orchestration des moteurs (hors-ligne) ────────────

_GEMINI_SAUVES: dict[str, object] = {}


def _sauve():
    _GEMINI_SAUVES["g"] = moteurs.gemini_generer
    _GEMINI_SAUVES["groq"] = moteurs.groq_generer


def _restaure():
    moteurs.gemini_generer = _GEMINI_SAUVES["g"]  # type: ignore[assignment]
    moteurs.groq_generer = _GEMINI_SAUVES["groq"]  # type: ignore[assignment]


def test_fallback_tous_echecs_moteur_indisponible():
    _sauve()
    moteurs.gemini_generer = lambda c: (_ for _ in ()).throw(  # noqa: E731
        moteurs.ErreurMoteur("quota", "gemini : quota dépassé.")
    )
    moteurs.groq_generer = lambda c: (_ for _ in ()).throw(  # noqa: E731
        moteurs.ErreurMoteur("auth", "groq : clé absente.")
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                agent_adapter.proposer_modifications("prompt", None, pathlib.Path(tmp))
            except ErreurAdaptateur as exc:
                assert exc.code == "moteur_indisponible"
                assert "quota" in exc.message and "auth" in exc.message
                return
        raise AssertionError("moteur_indisponible attendue")
    finally:
        _restaure()


def test_fallback_gemini_secours():
    _sauve()
    moteurs.gemini_generer = lambda c: '[{"action": "write", "fichier": "a.py", "contenu": "x=1\\n"}]'  # noqa: E731
    moteurs.groq_generer = lambda c: ""  # noqa: E731
    try:
        with tempfile.TemporaryDirectory() as tmp:
            moteur, brut = agent_adapter.proposer_modifications("prompt", None, pathlib.Path(tmp))
            assert moteur == "gemini"
            assert '"a.py"' in brut
    finally:
        _restaure()


def test_opencode_echoue_gemini_prend_le_relais():
    _sauve()
    moteurs.gemini_generer = lambda c: '[{"action": "write", "fichier": "b.py", "contenu": "y\\n"}]'  # noqa: E731
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # sid présent => opencode tenté, mais le serveur n'existe pas hors-ligne.
            moteur, brut = agent_adapter.proposer_modifications(
                "prompt", "sid-fake", pathlib.Path(tmp)
            )
            assert moteur == "gemini"
            assert '"b.py"' in brut
    finally:
        _restaure()


# ────────────────────────────────────────────────────────────────────────────

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