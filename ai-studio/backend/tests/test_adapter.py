"""Tests d'agent_adapter.py — parseur cascade + validation + orchestration.

Exécutables sans réseau ni serveur OpenCode ni clés LLM :
     python tests/test_adapter.py
"""

from __future__ import annotations

import pathlib
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


def test_decoupage_fichiers_limite_et_hors_projet():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        (racine / "gros.py").write_text("x" * 50000, encoding="utf-8")
        from app.config import FICHIER_CONTEXTE_MAX_CAR
        bloc = agent_adapter.decouper_bloc_fichiers(
            ["gros.py", "../sans.js"], racine
        )
    assert "gros.py" in bloc
    assert len(bloc) <= FICHIER_CONTEXTE_MAX_CAR + 40
    assert "sans.js" not in bloc


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