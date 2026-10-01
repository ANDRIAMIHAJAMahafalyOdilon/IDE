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
import time as _time

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


def _reponse(prompt):
    """Joue `stream_reponse` et renvoie (moteurs annoncés, deltas émis)."""
    evenements = list(agent_discussion.stream_reponse(prompt))
    return (
        [c for g, c in evenements if g == "moteur"],
        [c for g, c in evenements if g == "delta"],
    )


def test_gemini_prioritaire_et_agregation():
    sauv = _patch(lambda c: iter(["Bon", "jour"]), lambda c: iter(["jamais"]))
    try:
        assert _reponse("prompt") == (["gemini"], ["Bon", "jour"])
    finally:
        _restaure(sauv)


def test_bascule_groq_si_gemini_echoue_avant_token():
    def casse(c):
        raise ErreurMoteur("quota", "gemini : quota dépassé.")
        yield  # pragma: no cover — fait de la fonction un générateur

    sauv = _patch(casse, lambda c: iter(["secours"]))
    try:
        # Aucun delta n'ayant été émis, il n'y a rien à effacer : pas de reprise.
        assert _reponse("prompt") == (["groq"], ["secours"])
    finally:
        _restaure(sauv)


def test_flux_vide_passe_au_moteur_suivant():
    sauv = _patch(lambda c: iter([]), lambda c: iter(["ok"]))
    try:
        assert _reponse("prompt") == (["groq"], ["ok"])
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
            list(agent_discussion.stream_reponse("prompt"))
        except ErreurMoteur as exc:
            assert exc.code == "moteur_indisponible"
            assert "auth" in exc.message and "timeout" in exc.message
            return
        raise AssertionError("ErreurMoteur attendue")
    finally:
        _restaure(sauv)


def test_coupure_en_cours_de_flux_bascule_sans_coupure():
    """Exigence : aucune coupure visible pendant la discussion.

    Gemini s'interrompt APRÈS avoir envoyé des tokens. Le service doit
    basculer sur Groq, signaler `reprise` (le client efface alors le partiel)
    et diffuser une réponse complète — pas laisser une phrase coupée."""
    def mi_flux(c):
        yield "début de réponse"
        yield "qui s'inter"
        raise ErreurMoteur("timeout", "gemini : délai dépassé.")

    sauv = _patch(mi_flux, lambda c: iter(["réponse", "complète"]))
    try:
        evenements = list(agent_discussion.stream_reponse("prompt"))
    finally:
        _restaure(sauv)

    genres = [e[0] for e in evenements]
    assert genres == ["moteur", "delta", "delta", "reprise", "delta", "delta"]
    assert evenements[0] == ("moteur", "gemini")
    reprise = evenements[3]
    assert reprise[0] == "reprise"
    assert reprise[1][0] == "groq"
    assert "délai dépassé" in reprise[1][1]
    deltas = [c for g, c in evenements if g == "delta"]
    assert deltas == ["début de réponse", "qui s'inter", "réponse", "complète"]
    # Le client reçoit bien la fin du texte : aucun delta vide marquant l'arrêt.
    assert deltas[-1] == "complète"


def test_plus_de_reprise_emise_quand_le_secours_echoue():
    """Groq tombe aussi : plus rien à basculer, on remonte l'échec — et on ne
    signale PAS une seconde reprise (le client n'aurait rien à effacer)."""
    def coupe(c):
        yield "a"
        raise ErreurMoteur("quota", "gemini : quota.")
        yield  # pragma: no cover

    def coupe_aussi(c):
        yield "b"
        raise ErreurMoteur("timeout", "groq : délai.")
        yield  # pragma: no cover

    sauv = _patch(coupe, coupe_aussi)
    try:
        try:
            list(agent_discussion.stream_reponse("prompt"))
        except ErreurMoteur as exc:
            genres = []
            assert exc.code == "moteur_indisponible"
            assert "quota" in exc.message and "timeout" in exc.message
            return genres
        raise AssertionError("ErreurMoteur attendue")
    finally:
        _restaure(sauv)


def test_bascule_en_chaine_puis_echec_final():
    """Gemini stream puis coupe, Groq stream puis coupe : une SEULE reprise,
    puis l'échec remonte une fois le texte réellement reçu."""
    def coupe(nom, texte):
        def _f(c):
            yield texte
            raise ErreurMoteur("timeout", f"{nom} : délai.")
            yield  # pragma: no cover
        return _f

    sauv = _patch(coupe("gemini", "partiel"), coupe("groq", "secours"))
    genres: list[str] = []
    try:
        try:
            for e in agent_discussion.stream_reponse("prompt"):
                genres.append(e[0])
        except ErreurMoteur as exc:
            assert exc.code == "moteur_indisponible"
            assert "gemini" in exc.message and "groq" in exc.message
        else:
            raise AssertionError("ErreurMoteur attendue en fin de chaîne")
    finally:
        _restaure(sauv)
    # Un seul `reprise` : le client n'a rien à effacer une seconde fois.
    assert genres == ["moteur", "delta", "reprise", "delta"], genres


def test_moteur_vide_puis_secours_coupe_sans_reprise():
    """Gemini ne renvoie rien : aucun delta n'ayant été affiché, le passage à
    Groq ne demande AUCUNE reprise (le client n'a rien à effacer)."""
    def vide(c):
        return iter([])
        yield  # pragma: no cover

    def coupe(c):
        yield "secours partiel"
        raise ErreurMoteur("quota", "groq : quota.")
        yield  # pragma: no cover

    sauv = _patch(vide, coupe)
    genres: list[str] = []
    try:
        try:
            for e in agent_discussion.stream_reponse("prompt"):
                genres.append(e[0])
        except ErreurMoteur as exc:
            assert exc.code == "moteur_indisponible"
        else:
            raise AssertionError("ErreurMoteur attendue en fin de chaîne")
    finally:
        _restaure(sauv)
    assert genres == ["moteur", "delta"], genres


# ───────────────────── Événementiel /chat (mode chat) ────────────────────────

async def _collecte(gen):
    return [evt async for evt in gen]


def _evenements(events):
    return [e["event"] for e in events], [json.loads(e["data"]) for e in events]


def test_generer_discussion_debut_texte_fin_sans_proposition():
    sauv = agent_discussion.stream_reponse
    agent_discussion.stream_reponse = lambda p: iter(
        [("moteur", "gemini"), ("delta", "Bon"), ("delta", "jour")]
    )
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
        agent_discussion.stream_reponse = sauv
        agent._MEMOIRE.pop(("chat", "sess-chat-test"), None)
        nom = hashlib.sha256(b"chat:sess-chat-test").hexdigest() + ".json"
        (agent.MEMOIRE_DIR / nom).unlink(missing_ok=True)


def test_generer_discussion_reprise_efface_le_partiel():
    """Bout-en-bout : une coupure en cours de route produit `reprise`, et la
    mémoire du fil ne conserve QUE la réponse du moteur de secours — pas le
    texte tronqué du moteur mort."""
    def flux(p):
        yield ("moteur", "gemini")
        yield ("delta", "texte tron")
        yield ("reprise", ("groq", "gemini : délai dépassé."))
        yield ("delta", "réponse")
        yield ("delta", "complète")

    sauv = agent_discussion.stream_reponse
    agent_discussion.stream_reponse = flux
    agent._MEMOIRE.pop(("chat", "sess-reprise"), None)
    try:
        req = RequeteChat(mode="chat", message="salut", session="sess-reprise")
        noms, datas = _evenements(
            asyncio.run(_collecte(agent.generer_discussion(req)))
        )
        assert noms == ["debut", "texte", "reprise", "texte", "texte", "fin"]
        reprise = datas[noms.index("reprise")]
        assert reprise["moteur"] == "groq"
        assert "délai dépassé" in reprise["raison"]
        memoire = agent._memoire("chat", "sess-reprise")
        assert memoire[-1]["reponse"] == "réponsecomplète"
        assert "tron" not in memoire[-1]["reponse"]
    finally:
        agent_discussion.stream_reponse = sauv
        agent._MEMOIRE.pop(("chat", "sess-reprise"), None)
        nom = hashlib.sha256(b"chat:sess-reprise").hexdigest() + ".json"
        (agent.MEMOIRE_DIR / nom).unlink(missing_ok=True)


def test_generer_discussion_erreur_moteur():
    def casse(p):
        raise ErreurMoteur("quota", "gemini : quota dépassé.")
        yield  # pragma: no cover

    sauv = agent_discussion.stream_reponse
    agent_discussion.stream_reponse = casse
    agent._MEMOIRE.pop(("chat", "sess-err"), None)
    try:
        req = RequeteChat(mode="chat", message="salut", session="sess-err")
        noms, datas = _evenements(asyncio.run(_collecte(agent.generer_discussion(req))))
        assert noms == ["erreur"]
        assert datas[0]["code"] == "quota"
    finally:
        agent_discussion.stream_reponse = sauv
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


# ─────────────────────── Purge des dictionnaires globaux ─────────────────────

def test_purge_par_ttl_retire_les_entrees_perimees():
    agent._MEMOIRE.clear()
    agent._MEMOIRE_TS.clear()
    agent._memoire("chat", "vieux")
    agent._memoire("chat", "recent")
    # On vieillit « vieux » de plus que le TTL : la purge doit l'éliminer.
    agent._MEMOIRE_TS[("chat", "vieux")] -= agent.TTL_MEMOIRE_SECONDES + 1
    try:
        agent._purger()
        assert ("chat", "vieux") not in agent._MEMOIRE
        assert ("chat", "vieux") not in agent._MEMOIRE_TS
        assert ("chat", "recent") in agent._MEMOIRE
    finally:
        agent._MEMOIRE.clear()
        agent._MEMOIRE_TS.clear()


def test_purge_par_taille_supprime_les_plus_anciennes():
    agent._MEMOIRE.clear()
    agent._MEMOIRE_TS.clear()
    for i in range(5):
        agent._memoire("chat", f"s{i}")
    maximum = agent.MAX_ENTREES_MEMOIRE
    agent.MAX_ENTREES_MEMOIRE = 2
    try:
        agent._purger()
        # Les trois plus anciennes partent, les deux plus récentes restent.
        assert sorted(k[1] for k in agent._MEMOIRE) == ["s3", "s4"]
    finally:
        agent.MAX_ENTREES_MEMOIRE = maximum
        agent._MEMOIRE.clear()
        agent._MEMOIRE_TS.clear()


def test_purge_taches_preserve_les_taches_en_cours():
    agent._TACHES.clear()
    maximum = agent.MAX_ENTREES_MEMOIRE
    agent.MAX_ENTREES_MEMOIRE = 1
    try:
        active = agent.EtatTache("projet", "s1", "en cours")
        terminee = agent.EtatTache("projet", "s2", "finie")
        terminee.terminee = True
        terminee.creee -= 10_000  # ancienne ET terminée
        agent._TACHES[("projet", "s1")] = active
        agent._TACHES[("projet", "s2")] = terminee
        agent._purger()
        assert ("projet", "s1") in agent._TACHES  # jamais purgée en cours
        assert ("projet", "s2") not in agent._TACHES
    finally:
        agent.MAX_ENTREES_MEMOIRE = maximum
        agent._TACHES.clear()


def test_purge_taches_ne_purge_jamais_une_tache_active():
    """Régression : le tri par excédent de taille ne filtrait que
    « pas déjà périmé », sans exiger `terminee`. Avec plus de tâches actives que
    le plafond et aucune tâche terminée, la purge en retirait quand même, et
    le client ne pouvait plus se rattacher à une tâche EN COURS (`GET
    /agent/tache/{id}` -> 404) alors que le contrat l'interdit."""
    agent._TACHES.clear()
    maximum = agent.MAX_ENTREES_MEMOIRE
    agent.MAX_ENTREES_MEMOIRE = 2
    try:
        actives = []
        for i in range(5):
            etat = agent.EtatTache("projet", f"s{i}", "en cours")
            etat.creee -= 10_000  # anciennes, mais toujours en cours
            actives.append(("projet", f"s{i}"))
            agent._TACHES[("projet", f"s{i}")] = etat
        agent._purger()
        assert len(agent._TACHES) == 5, "aucune tâche active ne doit être purgée"
        assert all(cle in agent._TACHES for cle in actives)
    finally:
        agent.MAX_ENTREES_MEMOIRE = maximum
        agent._TACHES.clear()


def test_purge_taches_priorise_les_terminees_par_ordre_chronologique():
    agent._TACHES.clear()
    maximum = agent.MAX_ENTREES_MEMOIRE
    agent.MAX_ENTREES_MEMOIRE = 3
    try:
        # `creee` doit être exprimé sur l'HORLOGE MONOTONIQUE comme le fait
        # `_purger`, et non en 0, 1, 2, 3. Avec des valeurs arbitraires, le test
        # passait tant que la machine avait moins de TTL_MEMOIRE_SECONDES
        # (24 h) d'uptime — la limite était alors négative, donc aucune tâche
        # n'était purgée par TTL et seul le plafond de taille s'appliquait. Passé
        # 24 h d'uptime, la limite devient positive, toutes les tâches tombent
        # sous le seuil et le test échoue : il dépendait de l'âge de la machine.
        maintenant = _time.monotonic()
        for i in range(4):
            etat = agent.EtatTache("projet", f"s{i}", "finie")
            etat.terminee = True
            etat.creee = maintenant - (4 - i) * 10.0  # s0 la plus ancienne
            agent._TACHES[("projet", f"s{i}")] = etat
        agent._purger()
        # Une seule place à libérer : la plus ancienne part, les 3 autres restent.
        restants = sorted(k[1] for k in agent._TACHES)
        assert restants == ["s1", "s2", "s3"], restants
    finally:
        agent.MAX_ENTREES_MEMOIRE = maximum
        agent._TACHES.clear()


def test_purge_taches_ne_supprime_rien_si_sous_le_seuil():
    agent._TACHES.clear()
    try:
        etat = agent.EtatTache("projet", "s1", "finie")
        etat.terminee = True
        agent._TACHES[("projet", "s1")] = etat
        agent._purger()
        # Une tâche terminée reste rejouable tant que le TTL n'est pas atteint.
        assert ("projet", "s1") in agent._TACHES
    finally:
        agent._TACHES.clear()


def test_sessions_opencode_sont_purgees():
    agent._SESSIONS_OPENCODE.clear()
    agent._SESSIONS_OPENCODE_TS.clear()
    cle = ("projet", "sess")
    agent._SESSIONS_OPENCODE[cle] = "ses_1"
    agent._SESSIONS_OPENCODE_TS[cle] = agent._time.monotonic() - agent.TTL_MEMOIRE_SECONDES - 1
    try:
        agent._purger()
        assert cle not in agent._SESSIONS_OPENCODE
        assert cle not in agent._SESSIONS_OPENCODE_TS
    finally:
        agent._SESSIONS_OPENCODE.clear()
        agent._SESSIONS_OPENCODE_TS.clear()


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
