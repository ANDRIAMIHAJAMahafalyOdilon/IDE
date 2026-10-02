"""Mode « Chat » : discussion libre (Gemini/Groq streamé), aucune écriture.

Contrairement au mode Edit (`agent_adapter` + `build_diff`), ce service ne
produit JAMAIS de proposition : il streame du texte. Le contexte projet est
optionnel et en LECTURE SEULE. D'où l'absence totale de dépendance à
`diff`/`agent_adapter` : le mode Chat n'hérite d'aucun code d'erreur fichier.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterator

from . import moteurs, opencode, workspace
from ..config import (
    ARBRE_CONTEXTE_MAX,
    CONTEXTE_MAX_CAR,
    FICHIERS_CONTEXTE_MAX,
    MEMOIRE_ECHANGES_MAX,
    OPENCODE_CHAT_DIR,
)

# WARNING/ERROR et non INFO : aucune configuration de logging dans ce projet,
# racine uvicorn en WARNING. Voir le commentaire détaillé dans `moteurs.py`.
logger = logging.getLogger("ai_studio.discussion")

SYSTEME = (
    "Tu es un assistant intégré à un éditeur de code et un assistant d'étude. "
    "Tu réponds en texte, de façon claire et concise. Tu peux expliquer, "
    "analyser, comparer, donner des exemples ou des extraits de code. Tu ne "
    "modifies JAMAIS de fichier et tu ne produis aucune proposition "
    "d'édition : tu discutes avec l'utilisateur."
)


def construire_memoire(memoire: list[dict[str, Any]]) -> str:
    """Historique de la session Chat, formaté et tronqué pour le prompt."""
    morceaux = []
    for ech in list(memoire)[-MEMOIRE_ECHANGES_MAX:]:
        question = (ech.get("question") or "").strip()
        reponse = (ech.get("reponse") or "").strip()
        if question:
            morceaux.append(
                f"[utilisateur] {question[:1600]}\n[assistant] {reponse[:2600]}"
            )
    return "\n\n".join(morceaux)


def construire_contexte(
    racine: Path,
    fichiers_contexte: list[str] | None,
) -> tuple[str, str]:
    """(arborescence compacte, bloc des fichiers ouverts) — lecture seule."""
    arborescence = workspace.arborescence_texte(racine, limite=ARBRE_CONTEXTE_MAX)
    bloc_fichiers = ""
    if fichiers_contexte:
        chemins = [c for c in fichiers_contexte if c][:FICHIERS_CONTEXTE_MAX]
        if chemins:
            bloc_fichiers = workspace.bloc_fichiers_contexte(chemins, racine)
    return arborescence, bloc_fichiers


def _tronquer_prioritaire(
    blocs: list[tuple[str, str]], budget: int
) -> list[tuple[str, str]]:
    """Tronque des blocs (étiquette, texte) en gardant les prioritaires.

    Les blocs sont donnés du MOINS prioritaire au PLUS prioritaire : on les
    retire dans l'ordre tant que le total dépasse *budget*. Le dernier bloc
    (le plus prioritaire) ne suffit jamais en solo à dépasser => il reste.
    """
    gardes: list[tuple[str, str]] = []
    for etiquette, texte in blocs:
        if len(texte) > budget:
            # La fin contient les échanges les plus récents. Garder le début
            # faisait disparaître la dernière question lorsque le budget était
            # atteint.
            if etiquette.lower() == "mémoire":
                gardes.append(
                    (etiquette, "[…anciens échanges omis…]\n" + texte[-max(0, budget - 26):])
                )
            else:
                gardes.append((etiquette, texte[:budget]))
            break
        gardes.append((etiquette, texte))
        budget -= len(texte)
    return gardes


def construire_prompt(
    instruction: str,
    arborescence: str,
    bloc_fichiers: str,
    memoire: str,
    bloc_documents: str = "",
    bloc_web: str = "",
) -> str:
    """Prompt de discussion : jamais de consigne de format JSON.

    Le SYSTEME et la question sont TOUJOURS gardés intégralement. Seuls les
    blocs de contexte portent le poids du plafond CONTEXTE_MAX_CAR (≈ 3 800
    tokens, sous les 8 000 TPM de Groq) : on retire d'abord le web, puis les
    documents, la mémoire, et enfin les fichiers ouverts.
    """
    budget = max(
        0,
        CONTEXTE_MAX_CAR
        - len(SYSTEME)
        - len(instruction)
        - len(arborescence)
        - 40,  # en-têtes/labels fixes
    )
    blocs_midiens = [
        (etiquette, texte)
        for etiquette, texte in [
            ("Web", bloc_web),
            ("Documents", bloc_documents),
            ("Mémoire", memoire),
            ("Fichiers", bloc_fichiers),
        ]
        if texte
    ]
    retenus = _tronquer_prioritaire(blocs_midiens, budget)

    morceaux: list[str] = [f"{SYSTEME}\n\n", f"Arborescence du projet :\n{arborescence}\n"]
    for etiquette, texte in retenus:
        if etiquette == "Web" and texte:
            morceaux.append(f"\nRésultats de recherche web :\n{texte}\n")
        elif etiquette == "Documents" and texte:
            morceaux.append(f"\n{texte}\n")
        elif etiquette == "Mémoire" and texte:
            morceaux.append(f"\nConversation récente :\n{texte}\n")
        elif etiquette == "Fichiers" and texte:
            morceaux.append(f"\nFichiers ouverts dans l'éditeur :\n{texte}\n")
    morceaux.append(f"\nQuestion de l'utilisateur :\n{instruction}\n")

    prompt = "".join(morceaux)
    if len(prompt) > CONTEXTE_MAX_CAR:
        prompt = (
            prompt[: len(SYSTEME) + 1]
            + "\n[…contexte tronqué…] "
            + prompt[-(CONTEXTE_MAX_CAR - len(SYSTEME) - len(instruction) - 20) :].lstrip()
        )
    return prompt


def construire_bloc_documents(question: str) -> str:
    """Passages de cours pertinents (RAG FAISS) — vide si indisponible.

    Ancre la réponse sur le contenu des documents indexés : le moteur est
    invité à répondre en priorité avec ces passages et à citer leur source.
    """
    from .rag import embeddings as rag_emb

    try:
        passages = rag_emb.rechercher(question)
    except Exception:  # noqa: BLE001 — le Chat fonctionne sans documents
        return ""
    if not passages:
        return ""

    blocs = []
    for p in passages:
        source = str(p.get("source") or "(documents)").strip()
        page = p.get("page")
        lieu = f"{source} — page {page}" if page else source
        texte = (p.get("texte") or "").strip()
        if texte:
            blocs.append(f"[{lieu}]\n{texte}\n")

    if not blocs:
        return ""
    return (
        "Passages extraits de TES documents de cours, apparentés à la question "
        "de l'utilisateur. Réponds en priorité avec ces passages et cite leur "
        "origine à la fin, du style « Source : <fichier>, page X » :\n\n"
        + "\n\n".join(blocs)
    )


def construire_bloc_web(question: str) -> str:
    """Résultats de recherche web DuckDuckGo — vide si indisponible."""
    from .rag import web as rag_web

    try:
        resultats = rag_web.rechercher(question)
    except Exception:  # noqa: BLE001 — le Chat fonctionne sans web
        return ""
    lignes = []
    for r in resultats:
        titre = r.get("titre") or "(sans titre)"
        url = r.get("url") or ""
        ligne = f"- {titre} ({url})"
        if r.get("extrait"):
            ligne += f" — {r['extrait'][:220]}"
        lignes.append(ligne)
    if not lignes:
        return ""
    return (
        "Résultats d'une recherche web réalisée sur la question. Utilise-les "
        "uniquement s'ils sont pertinents et cite alors l'URL exacte de chaque "
        "source :\n"
        + "\n".join(lignes)
    )


def _premier_delta(flux: Iterator[str]) -> str | None:
    """Consomme le flux jusqu'au premier token non vide (None si flux vide)."""
    for delta in flux:
        if delta:
            return delta
    return None


MoteurFlux = Callable[[str], Iterator[str]]


def _tronconner(texte: str, taille: int = 80) -> Iterator[str]:
    """Découpe une réponse complète en fragments pour l'affichage progressif.

    Les coupures se font sur une espace ou un retour à la ligne, jamais au
    caractère près : une coupure en plein milieu d'un mot s'affiche comme du
    texte incohérent alors que la réponse, elle, est parfaitement correcte. Les
    espaces sont conservés, donc la concaténation redonne exactement `texte`.
    """
    reste = texte
    while reste:
        if len(reste) <= taille:
            yield reste
            return
        fenetre = reste[:taille]
        coupe = max(fenetre.rfind(" "), fenetre.rfind("\n"))
        if coupe <= 0:
            # Aucun mot court dans la fenêtre : un texte sans espace (chemin,
            # code collé) n'a pas de frontière à respecter.
            coupe = taille
        yield reste[: coupe + 1]
        reste = reste[coupe + 1 :]


def _flux_opencode(prompt: str) -> Iterator[str]:
    """Réponse du moteur OpenCode, découpée pour l'affichage progressif.

    `repondre_chat` rend la réponse complète (l'endpoint message ne streame pas).
    Le découpage restitue une progression lisible sans parser le flux SSE, dont
    la gestion des parties a déjà causé des doublons de texte par le passé.
    `ErreurOpenCode` est traduite en `ErreurMoteur` : sans cela, la bascule vers
    les moteurs suivants de `stream_reponse` ne se ferait pas.
    """
    try:
        texte = opencode.repondre_chat(OPENCODE_CHAT_DIR, prompt)
    except opencode.ErreurOpenCode as exc:
        raise moteurs.ErreurMoteur("moteur_indisponible", f"opencode : {exc.message}") from exc
    if not texte:
        raise moteurs.ErreurMoteur("moteur_indisponible", "opencode : réponse vide.")
    yield from _tronconner(texte)


def _engins() -> list[tuple[str, MoteurFlux]]:
    """Chaîne de repli du Chat, résolue à chaque appel.

    Volontairement construite à la volée et non figée au chargement du module :
    une liste constante capturerait les objets fonction d'origine, et toute
    substitution de `moteurs.gemini_flux` (tests, bascule de moteur) serait
    silencieusement ignorée.

    OpenCode est EN TÊTE, toujours, sans condition de clé.

    L'utilisateur attend « je réponds avec le moteur embarqué, gratuitement ».
    La présence d'une clé Gemini dans le .env ne doit pas suffire à détourner la
    conversation vers un service distant payant ou quota-limité : cette clé sert
    à la recherche web et à l'indexation des documents, pas à choisir le moteur
    d'une simple réponse.

    Gemini et Groq restent en SECOURS — ce qu'ils étaient avant que le moteur
    embarqué existe. Ils ne sont tentés que si OpenCode échoue, ce qui laisse la
    bascule intacte sans jamais en faire le choix par défaut.
    """
    return [
        ("opencode", _flux_opencode),
        ("gemini", moteurs.gemini_flux),
        ("groq", moteurs.groq_flux),
    ]


def stream_reponse(prompt: str) -> Iterator[tuple[str, Any]]:
    """Diffuse la réponse avec basculement automatique, SANS coupure.

    Rend des couples ``(type, charge)`` :

    ``("moteur", nom)``
        Premier moteur retenu, émis une seule fois, avant tout token.
    ``("delta", texte)``
        Fragment de réponse à afficher.
    ``("reprise", (nouveau_moteur, raison))``
        Le moteur courant a lâché APRÈS avoir déjà streamed. L'appelant doit
        alors EFFACER les deltas déjà reçus et repartir de zéro : c'est ce qui
        évite qu'une réponse tronquée soit collée devant la réponse du moteur
        de secours. Aucun ``("moteur")`` n'est réémis, ``reprise`` porte déjà
        le nouveau nom (l'interface met à jour son étiquette avec).

    Le basculement ne se limite donc pas au tout début : une coupure réseau,
    un quota dépassé ou un délai dépassé en pleine génération relancent la
    réponse depuis le début avec le moteur suivant. Le seul cas non récupérable
    est l'épuisement de la chaîne, qui lève `ErreurMoteur`.
    """
    erreurs: list[str] = []
    engins = _engins()
    i = 0
    annonce = False
    while i < len(engins):
        nom, flux_fn = engins[i]
        # ── Sélection du moteur : jusqu'au premier token inclus ──────────────
        try:
            flux = flux_fn(prompt)
            premier = _premier_delta(flux)
        except moteurs.ErreurMoteur as exc:
            erreurs.append(f"{exc.code} ({nom}) : {exc.message}")
            logger.error(
                "moteur_refuse moteur=%s code=%s avant_premier_chunk=oui",
                nom,
                exc.code,
            )
            i += 1
            continue
        if premier is None:
            erreurs.append(f"{nom} : réponse vide.")
            i += 1
            continue

        if not annonce:
            yield ("moteur", nom)
            annonce = True

        # ── Diffusion, avec sortie de secours si le flux se coupe ───────────
        abandonnes = 1  # le premier delta a déjà été yieldé
        try:
            yield ("delta", premier)
            for delta in flux:
                if delta:
                    abandonnes += 1
                    yield ("delta", delta)
        except moteurs.ErreurMoteur as exc:
            erreurs.append(f"{exc.code} ({nom}) : {exc.message}")
            i += 1
            reste = engins[i:]
            if reste:
                # WARNING et non INFO : visible dans uvicorn.log sans
                # configuration de logging (cf. commentaire de moteurs.py).
                # Ni le prompt ni les chunks abandonnés ne sont journalisés.
                logger.warning(
                    "bascule de=%s vers=%s code=%s chunks_abandones=%d",
                    nom,
                    reste[0][0],
                    exc.code,
                    abandonnes,
                )
                yield ("reprise", (reste[0][0], f"{nom} : {exc.message}"))
            continue
        return

    logger.error("chaine_epuisee erreurs=%d", len(erreurs))
    raise moteurs.ErreurMoteur(
        "moteur_indisponible",
        "Aucun moteur n'a produit de réponse : " + " | ".join(erreurs),
    )
