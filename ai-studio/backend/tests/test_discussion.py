"""Tests du mode « Chat » (services/agent_discussion.py + branche /chat).

Exécutables sans réseau, sans clé LLM et sans serveur OpenCode :
     python tests/test_discussion.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.api import agent  # noqa: E402
from app.models.chat import RequeteChat  # noqa: E402
from app.services import agent_discussion, moteurs  # noqa: E402
from app.services.agent_discussion import (  # noqa: E402
    construire_contexte,
    construire_memoire,
    construire_prompt,
)
from app.services.moteurs import ErreurMoteur  # noqa: E402


# ─────────────────────────────── Prompt / mémoire (pur) ──────────────────────

def test_prompt_contient_systeme_et_pas_de_consigne_json():
    prompt = construire_prompt("Explique ce projet.", "(projet vide)", "", "")
    assert "assistant" in prompt.lower()
    assert "Explique ce projet." in prompt
    assert "JSON" not in prompt
    assert "action" not in prompt.lower()


def test_prompt_injecte_contexte_et_memoire():
    prompt = construire_prompt(
        "question", "a.py\nb.py", "===FICHIER a.py===\nx=1", "[utilisateur] q\n[assistant] r"
    )
    assert "a.py" in prompt and "===FICHIER a.py===" in prompt
    assert "Conversation récente" in prompt


def test_memoire_tronquee_et_formatee():
    memoire = [{"question": f"q{i}", "reponse": f"r{i}"} for i in range(50)]
    texte = construire_memoire(memoire)
    assert "[utilisateur] q49" in texte
    assert "[assistant] r49" in texte
    assert "q0" not in texte  # seuls les derniers échanges sont gardés


def test_contexte_lecture_seule_sans_ecriture():
    with tempfile.TemporaryDirectory() as tmp:
        racine = pathlib.Path(tmp)
        (racine / "a.py").write_text("x = 1\n", encoding="utf-8")
        (racine / "sous").mkdir()
        (racine / "sous" / "b.py").write_text("y = 2\n", encoding="utf-8")
        arbo, bloc = construire_contexte(racine, ["a.py"])
        contenu_avant = (racine / "a.py").read_text(encoding="utf-8")
    assert "a.py" in arbo and "b.py" in arbo
    assert "===FICHIER a.py===" in bloc
    assert contenu_avant == "x = 1\n"  # le fichier n'a pas été touché


# ───────────────────────────── Cascade de moteurs ────────────────────────────

def _patch(flux_gemini, flux_groq):
    sauv = (moteurs.gemini_flux, moteurs.groq_flux)
    moteurs.gemini_flux = flux_gemini
    moteurs.groq_flux = flux_groq
    return sauv


def _restaure(sauv):
    moteurs.gemini_flux, moteurs.groq_flux = sauv


def test_gemini_prioritaire_et_agregation():
    sauv = _patch(lambda c: iter(["Bon", "jour"]), lambda c: iter(["jamais"]))
    try:
        moteur, flux = agent_discussion.demarrer_reponse("prompt")
        assert moteur == "gemini"
        assert "".join(flux) == "Bonjour"
    finally:
        _restaure(sauv)


def test_bascule_groq_si_gemini_echoue_avant_token():
    def casse(c):
        raise ErreurMoteur("quota", "gemini : quota dépassé.")
        yield  # pragma: no cover — fait de la fonction un générateur

    sauv = _patch(casse, lambda c: iter(["secours"]))
    try:
        moteur, flux = agent_discussion.demarrer_reponse("prompt")
        assert moteur == "groq"
        assert "".join(flux) == "secours"
    finally:
        _restaure(sauv)


def test_flux_vide_passe_au_moteur_suivant():
    sauv = _patch(lambda c: iter([]), lambda c: iter(["ok"]))
    try:
        moteur, flux = agent_discussion.demarrer_reponse("prompt")
        assert moteur == "groq"
        assert "".join(flux) == "ok"
    finally:
        _restaure(sauv)


def test_tous_echecs_leve_moteur_indisponible():
    def casse(code):
        def _g(c):
            raise ErreurMoteur(code, f"{code} : échec")
            yield  # pragma: no cover
        return _g

    sauv = _patch(casse("auth"), casse("timeout"))
    try:
        try:
            agent_discussion.demarrer_reponse("prompt")
        except ErreurMoteur as exc:
            assert exc.code == "moteur_indisponible"
            assert "auth" in exc.message and "timeout" in exc.message
            return
        raise AssertionError("ErreurMoteur attendue")
    finally:
        _restaure(sauv)


def test_erreur_en_cours_de_flux_propage():
    def mi_flux(c):
        yield "début"
        raise ErreurMoteur("timeout", "gemini : délai dépassé.")

    sauv = _patch(mi_flux, lambda c: iter(["jamais"]))
    try:
        moteur, flux = agent_discussion.demarrer_reponse("prompt")
        assert moteur == "gemini"
        try:
            "".join(flux)
        except ErreurMoteur as exc:
            assert exc.code == "timeout"
            return
        raise AssertionError("ErreurMoteur attendue")
    finally:
        _restaure(sauv)


# ───────────────────── Événementiel /chat (mode chat) ────────────────────────

async def _collecte(gen):
    return [evt async for evt in gen]


def _evenements(events):
    return [e["event"] for e in events], [json.loads(e["data"]) for e in events]


def test_generer_discussion_debut_texte_fin_sans_proposition():
    sauv = agent_discussion.demarrer_reponse
    agent_discussion.demarrer_reponse = lambda p: ("gemini", iter(["Bon", "jour"]))
    agent._MEMOIRE.pop(("chat", "sess-chat-test"), None)
    try:
        req = RequeteChat(mode="chat", message="salut", session="sess-chat-test")
        events = asyncio.run(_collecte(agent.generer_discussion(req)))
        noms, datas = _evenements(events)
        assert noms == ["debut", "texte", "texte", "fin"]
        assert "proposition" not in noms
        assert datas[0]["moteur"] == "gemini"
        assert datas[0]["session"] == "sess-chat-test"
        assert datas[1]["delta"] == "Bon" and datas[2]["delta"] == "jour"
        memoire = agent._memoire("chat", "sess-chat-test")
        assert memoire[-1]["question"] == "salut"
        assert memoire[-1]["reponse"] == "Bonjour"
    finally:
        agent_discussion.demarrer_reponse = sauv
        agent._MEMOIRE.pop(("chat", "sess-chat-test"), None)
        nom = hashlib.sha256(b"chat:sess-chat-test").hexdigest() + ".json"
        (agent.MEMOIRE_DIR / nom).unlink(missing_ok=True)


def test_generer_discussion_erreur_moteur():
    def casse(p):
        raise ErreurMoteur("quota", "gemini : quota dépassé.")

    sauv = agent_discussion.demarrer_reponse
    agent_discussion.demarrer_reponse = casse
    agent._MEMOIRE.pop(("chat", "sess-err"), None)
    try:
        req = RequeteChat(mode="chat", message="salut", session="sess-err")
        noms, datas = _evenements(asyncio.run(_collecte(agent.generer_discussion(req))))
        assert noms == ["erreur"]
        assert datas[0]["code"] == "quota"
    finally:
        agent_discussion.demarrer_reponse = sauv
        agent._MEMOIRE.pop(("chat", "sess-err"), None)


# ───────────────────── Contexte optionnel + isolation des fils ───────────────

def test_chat_sans_projet_aucun_contexte():
    arbo, bloc = agent._contexte_discussion(RequeteChat(mode="chat", message="x"))
    assert (arbo, bloc) == ("", "")


def test_projet_inconnu_ignore_pour_le_chat():
    req = RequeteChat(mode="chat", message="x", projet="projet-inexistant-xyz-123")
    assert agent._contexte_discussion(req) == ("", "")


def test_sessions_isolatees_par_mode():
    agent._MEMOIRE.clear()
    sid_chat = agent._session_id("chat", "partage")
    sid_edit = agent._session_id("edit", "partage")
    agent._memoire("chat", sid_chat).append({"question": "q", "reponse": "r"})
    assert agent._memoire("chat", sid_chat) != []
    assert agent._memoire("edit", sid_edit) == []  # aucun mélange de fils
    agent._MEMOIRE.clear()


def test_memoire_rechargee_apres_redemarrage_backend():
    sid = "sess-persist-test"
    ancienne = agent._MEMOIRE.pop(("chat", sid), None)
    memoire = agent._memoire("chat", sid)
    memoire.clear()
    memoire.append({"question": "à retenir", "reponse": "réponse"})
    agent._sauver_memoire("chat", sid, memoire)
    agent._MEMOIRE.pop(("chat", sid), None)
    try:
        assert agent._session_id("chat", sid) == sid
        assert agent._memoire("chat", sid)[-1]["question"] == "à retenir"
    finally:
        agent._MEMOIRE.pop(("chat", sid), None)
        nom = hashlib.sha256(f"chat:{sid}".encode("utf-8")).hexdigest() + ".json"
        (agent.MEMOIRE_DIR / nom).unlink(missing_ok=True)
        if ancienne is not None:
            agent._MEMOIRE[("chat", sid)] = ancienne


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
