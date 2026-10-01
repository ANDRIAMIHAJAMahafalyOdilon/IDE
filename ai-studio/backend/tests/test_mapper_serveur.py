"""Tests du mapper serveur : anti-raisonnement et armement d'étape.

    python tests/test_mapper_serveur.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_tache as at  # noqa: E402

RACINE = pathlib.Path("C:/projet")


def delta(part_id: str, message_id: str, texte: str) -> dict:
    return {
        "type": "message.part.delta",
        "properties": {
            "sessionID": "s1",
            "messageID": message_id,
            "partID": part_id,
            "field": "text",
            "delta": texte,
        },
    }


def outil_part(message_id: str = "m1", part_id: str = "p9") -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "s1",
            "part": {
                "id": part_id,
                "messageID": message_id,
                "type": "tool",
                "tool": "bash",
                "callID": "c1",
                "state": {"status": "running", "input": {"command": "npm test"}},
            },
        },
    }


def raison(message_id: str = "m1", part_id: str = "p0") -> dict:
    return {
        "type": "message.part.delta",
        "properties": {
            "sessionID": "s1",
            "messageID": message_id,
            "partID": part_id,
            "field": "reasoning",
            "delta": "je vais lancer la commande",
        },
    }


def texte_visible(sorties: list[tuple[str, dict]]) -> str:
    return "".join(d.get("delta", "") for nom, d in sorties if nom == "texte")


def test_le_mapper_serveur_ne_plante_pas_sur_step_started():
    """Regression : `etat_activites` n'etait pas defini dans ce mapper."""
    evt = {"type": "session.next.step.started", "properties": {"sessionID": "s1"}}
    sorties = at.mapper_evenement_serveur(evt, RACINE, {})
    assert sorties and sorties[0][1]["id"] == "step:s1:1"


def test_une_annonce_precedant_un_outil_est_ecartee():
    o = {"__roles__": {"m1": "assistant"}}
    at.mapper_evenement_serveur(raison(), RACINE, o)
    # L'agent annonce puis agit : son annonce ne doit pas atteindre l'utilisateur.
    accumule = []
    accumule += at.mapper_evenement_serveur(delta("p1", "m1", "Je lance le test."), RACINE, o)
    accumule += at.mapper_evenement_serveur(outil_part(), RACINE, o)
    assert texte_visible(accumule) == ""


def test_le_raisonnement_reste_visible_dans_la_timeline():
    o = {"__roles__": {"m1": "assistant"}}
    sorties = at.mapper_evenement_serveur(raison(), RACINE, o)
    pensees = [d for nom, d in sorties if nom == "activite"]
    assert pensees and pensees[0]["type"] == "thinking"
    assert "lancer la commande" in pensees[0]["description"]


def test_une_reponse_sans_outil_est_bien_affichee():
    o = {"__roles__": {"m1": "assistant"}}
    sorties = at.mapper_evenement_serveur(delta("p1", "m1", "Voici la reponse."), RACINE, o)
    assert texte_visible(sorties) == "Voici la reponse."


def test_deux_textes_sans_action_ont_tous_les_deux_passer():
    o = {"__roles__": {"m1": "assistant"}}
    etat = {}
    etat[CLE] = True  # arme comme apres une etape ouverte
    o = {"__roles__": {"m1": "assistant"}, "__activites__": {CLE: True}}
    sorties = []
    sorties += at.mapper_evenement_serveur(delta("p1", "m1", "Premiere partie. "), RACINE, o)
    sorties += at.mapper_evenement_serveur(delta("p2", "m1", "Seconde partie."), RACINE, o)
    assert texte_visible(sorties) == "Premiere partie. Seconde partie."


CLE = at.CLE_ARME


def test_une_reponse_apres_un_outil_nest_pas_jetsee():
    """Regression : l'index de lecture partage peut avaler la vraie reponse."""
    o = {"__roles__": {"m1": "assistant"}}
    sorties = []
    sorties += at.mapper_evenement_serveur(outil_part(), RACINE, o)
    sorties += at.mapper_evenement_serveur(delta("p1", "m1", "Le test passe."), RACINE, o)
    assert texte_visible(sorties) == "Le test passe."


def test_le_snapshot_complet_ne_duplique_pas_le_texte():
    o = {"__roles__": {"m1": "assistant"}}
    sorties = []
    sorties += at.mapper_evenement_serveur(delta("p1", "m1", "Bonjour"), RACINE, o)
    snapshot = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "s1",
            "part": {"id": "p1", "messageID": "m1", "type": "text", "text": "Bonjour"},
        },
    }
    sorties += at.mapper_evenement_serveur(snapshot, RACINE, o)
    assert texte_visible(sorties) == "Bonjour"


def test_une_nouvelle_etape_rearme_la_detection():
    o = {"__roles__": {"m1": "assistant"}}
    sorties = []
    sorties += at.mapper_evenement_serveur(outil_part(), RACINE, o)
    sorties += at.mapper_evenement_serveur(
        {"type": "session.next.step.started", "properties": {"sessionID": "s1"}}, RACINE, o
    )
    # L'annonce de l'etape suivante est de nouveau jetable si un outil suit.
    sorties += at.mapper_evenement_serveur(delta("p2", "m1", "Je verifie."), RACINE, o)
    sorties += at.mapper_evenement_serveur(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "s1",
                "part": {
                    "id": "p8", "messageID": "m1", "type": "tool", "tool": "read",
                    "callID": "c2", "state": {"status": "running", "input": {}},
                },
            },
        },
        RACINE,
        o,
    )
    assert texte_visible(sorties) == ""


if __name__ == "__main__":
    tests = [
        test_le_mapper_serveur_ne_plante_pas_sur_step_started,
        test_une_annonce_precedant_un_outil_est_ecartee,
        test_le_raisonnement_reste_visible_dans_la_timeline,
        test_une_reponse_sans_outil_est_bien_affichee,
        test_deux_textes_sans_action_ont_tous_les_deux_passer,
        test_une_reponse_apres_un_outil_nest_pas_jetsee,
        test_le_snapshot_complet_ne_duplique_pas_le_texte,
        test_une_nouvelle_etape_rearme_la_detection,
    ]
    ok = 0
    for test in tests:
        try:
            test()
            print(f"  [OK] {test.__name__}")
            ok += 1
        except AssertionError as exc:
            print(f"  [ECHEC] {test.__name__} : {exc}")
    print(f"{ok}/{len(tests)} tests reussis")
    raise SystemExit(0 if ok == len(tests) else 1)
