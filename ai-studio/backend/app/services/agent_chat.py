"""Orchestration du chat agent + construction des propositions en hunks (SSE).

Deux chemins :
- `creer_propositions` : mode simulation (contenus bruts fournis par le
  frontend) — la chaîne build_diff -> hunks -> SSE, développée en premier ;
- `generer_propositions_moteur` : le vrai moteur (OpenCode proposeur-seul,
  fallback Gemini puis Groq). Le contexte est construit CÔTÉ BACKEND
  (arborescence compacte + fichiers_contexte + mémoire de session), la sortie
  brute passe par l'adaptateur (JSON/===FILE=== -> validation -> build_diff).

Dans les deux cas le frontend ne reçoit que des hunks, jamais de contenu brut
(décision d'architecture : build_diff est appelé côté backend uniquement).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from . import workspace
from . import agent_adapter
from ..config import (
    ARBRE_CONTEXTE_MAX,
    CONTEXTE_EDIT_MAX_CAR,
    FICHIER_CONTEXTE_MAX_CAR,
    FICHIER_CONTEXTE_MAX_LIGNES,
    FICHIER_EDIT_MAX_CAR,
    FICHIER_EDIT_MAX_LIGNES,
    FICHIERS_CONTEXTE_MAX,
    MEMOIRE_ECHANGES_MAX,
)
from .diff import build_diff


def creer_propositions(
    racine: Path,
    simulations: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convertit des contenus bruts {'chemin', 'contenu'} en propositions hunks."""
    propositions: list[dict[str, Any]] = []
    for sim in simulations:
        rel = str(sim["chemin"])
        nouveau = str(sim["contenu"])
        chemin = workspace.chemin_securise(racine, rel)
        ancien = workspace.lire_fichier_ou(chemin, "")
        if ancien == nouveau:
            continue
        diff = build_diff(ancien, nouveau)
        propositions.append(
            {
                "fichier": rel,
                "action": "write",
                "source_hash": diff["source_hash"],
                "hunks": diff["hunks"],
                "stats": diff["stats"],
            }
        )
    return propositions


def _proposition_suppression(racine: Path, fichier: str) -> dict[str, Any]:
    """Proposition `delete` : hunks synthétiques (tout le fichier en `del`)
    pour l'affichage + le cadenas d'acceptation. L'application ne garde que
    l'action `delete` (les hunks servent à valider visuellement)."""
    chemin = workspace.chemin_securise(racine, fichier)
    ancien = workspace.lire_fichier_ou(chemin, "")
    diff = build_diff(ancien, "")
    return {
        "fichier": fichier,
        "action": "delete",
        "source_hash": diff["source_hash"],
        "hunks": diff["hunks"],
        "stats": diff["stats"],
    }


def _construire_memoire(memoire: list[dict[str, Any]]) -> str:
    """Mémoire de session (derniers échanges, tronqués) pour le prompt."""
    morceaux = []
    for ech in list(memoire)[-MEMOIRE_ECHANGES_MAX:]:
        question = (ech.get("question") or "").strip()
        reponse = (ech.get("reponse") or "").strip()
        if question:
            morceaux.append(
                f"[utilisateur] {question[:1600]}\n"
                f"[agent] {reponse[:2600]}"
            )
    return "\n\n".join(morceaux)


def _construire_contexte(
    racine: Path,
    fichiers_contexte: list[str] | None,
    mode_edit: bool = False,
) -> tuple[str, str]:
    """(arborescence compacte, bloc des fichiers de contexte limité).

    `mode_edit` bascule sur des plafonds bien plus larges : voir
    `appliquer_patch`, dont les numéros de ligne n'existent que si la zone à
    modifier est réellement présente dans le contexte.
    """
    arborescence = workspace.arborescence_texte(racine, limite=ARBRE_CONTEXTE_MAX)
    bloc_fichiers = ""
    if fichiers_contexte:
        chemins = [c for c in fichiers_contexte if c][: FICHIERS_CONTEXTE_MAX]
        if chemins:
            bloc_fichiers = workspace.bloc_fichiers_contexte(
                chemins,
                racine,
                limite_car=FICHIER_EDIT_MAX_CAR if mode_edit else FICHIER_CONTEXTE_MAX_CAR,
                limite_lignes=(
                    FICHIER_EDIT_MAX_LIGNES if mode_edit else FICHIER_CONTEXTE_MAX_LIGNES
                ),
            )
    return arborescence, bloc_fichiers


def generer_propositions_moteur(
    racine: Path,
    instruction: str,
    fichiers_contexte: list[str] | None,
    memoire: list[dict[str, Any]],
    sid_opencode: str | None,
) -> dict[str, Any]:
    """Propose des modifications via le vrai moteur.

    Retourne {moteur, texte_resume, propositions}. Lève ErreurAdaptateur
    (parse/validation/moteur) — l'API le convertit en événement `erreur`.
    """
    arborescence, bloc_fichiers = _construire_contexte(
        racine, fichiers_contexte, mode_edit=True
    )
    prompt = agent_adapter.construire_prompt(
        instruction,
        arborescence,
        bloc_fichiers,
        _construire_memoire(memoire),
        budget_car=CONTEXTE_EDIT_MAX_CAR,
    )
    moteur, brut = agent_adapter.proposer_modifications(prompt, sid_opencode, racine)

    bruts = agent_adapter.parser_propositions(brut, racine)
    propositions: list[dict[str, Any]] = []
    for prop in bruts:
        fichier = prop["fichier"]
        if prop["action"] == "delete":
            propositions.append(_proposition_suppression(racine, fichier))
            continue
        chemin = workspace.chemin_securise(racine, fichier)
        ancien = workspace.lire_fichier_ou(chemin, "")
        if prop["action"] == "patch":
            # Le patch ne porte que les lignes modifiées : c'est le contenu RÉEL
            # du disque qui sert de base, pas celui renvoyé par l'agent. Sans
            # cela, un fichier tronqué en contexte produirait une proposition qui
            # effacerait tout ce que le modèle n'a pas vu.
            try:
                nouveau = workspace.appliquer_patch(ancien, prop["operations"])
            except workspace.PatchInvalide as exc:
                raise agent_adapter.ErreurAdaptateur(
                    "patch_invalide", str(exc), fichier=fichier,
                ) from exc
        else:
            nouveau = str(prop["contenu"])
        if ancien == nouveau:
            continue
        diff = build_diff(ancien, nouveau)
        propositions.append(
            {
                "fichier": fichier,
                "action": "write",
                "source_hash": diff["source_hash"],
                "hunks": diff["hunks"],
                "stats": diff["stats"],
            }
        )
    texte_resume = _resumer_sortie(brut)
    return {
        "moteur": moteur,
        "texte_resume": texte_resume,
        "propositions": propositions,
    }


def _resumer_sortie(brut: str) -> str:
    """Résumé textuel du moteur : uniquement s'il y a une explication hors du
    JSON (sinon les propositions suffisent et le JSON brut serait du bruit)."""
    if agent_adapter._extraire_json_value(brut) is not None:
        return ""
    return brut[:300].strip()