"""Adaptateur entre la sortie brute du moteur d'agent et build_diff.

Le vrai moteur (OpenCode ou Gemini/Groq) ne renvoie PAS un JSON propre :
il produit du texte, du markdown, des explications autour du JSON, etc.
Cet adaptateur :

  1. construit le prompt (contexte + consigne de format strict) ;
  2. appelle les moteurs dans l'ordre OpenCode -> Gemini -> Groq ;
  3. parse la sortie en cascade — L1 (JSON strict) puis L2 (protocole
     texte ===FILE/===DELETE ===) puis L3 (erreur structurée `parse_format`) ;
  4. valide le schéma de chaque proposition (action, fichier requis, chemin
     sécurisé, contenu pour `write`) avant de la transmettre à build_diff.

Tout échec remonte via ErreurAdaptateur {code, message, fichier?} :
jamais de fermeture silencieuse du flux, et jamais de proposition floue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from . import workspace
from . import opencode, moteurs
from ..config import CONTEXTE_MAX_CAR


def _tronquer_prioritaire(
    blocs: list[tuple[str, str]], budget: int
) -> list[tuple[str, str]]:
    """Tronque des blocs (étiquette, texte), du MOINS au PLUS prioritaire.

    Retire les blocs dans l'ordre tant que le total dépasse *budget* ; le
    dernier bloc (le plus prioritaire) reste toujours (tronqué si besoin).
    """
    gardes: list[tuple[str, str]] = []
    for etiquette, texte in blocs:
        if len(texte) > budget:
            if etiquette.lower() == "memoire":
                gardes.append(
                    (etiquette, "[…anciens échanges omis…]\n" + texte[-max(0, budget - 26):])
                )
            else:
                gardes.append((etiquette, texte[:budget]))
            break
        gardes.append((etiquette, texte))
        budget -= len(texte)
    return gardes

# ── Protocole texte ===FILE …===END=== / ===DELETE …=== (hérité de workspace_edit). ──
RE_FICHIER = re.compile(
    r"===FILE\s+([^\n=]+?)===\s*\n(.*?)\n===END===", re.DOTALL | re.IGNORECASE
)
RE_DELETE = re.compile(r"===DELETE\s+([^\n=]+?)===", re.IGNORECASE)


class ErreurAdaptateur(Exception):
    """Erreur d'adaptateur, avec code normalisé pour l'événement SSE `erreur`."""

    def __init__(self, code: str, message: str, fichier: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fichier = fichier


# ────────────────────────────────────── Parsing (L1 / L2) ────────────────────

def _extraire_json_value(texte: str) -> Any | None:
    """Extrait la première valeur JSON (objet ou tableau) du texte, même
    entourée de markdown/prose. Retourne None si aucun JSON valide."""
    for ouvreur, fermeur in (("[", "]"), ("{", "}")):
        debut = 0
        while True:
            debut = texte.find(ouvreur, debut)
            if debut == -1:
                break
            profondeur = 0
            entre_chaine = False
            echappe = False
            i = debut
            while i < len(texte):
                c = texte[i]
                if entre_chaine:
                    if echappe:
                        echappe = False
                    elif c == "\\":
                        echappe = True
                    elif c == '"':
                        entre_chaine = False
                else:
                    if c == '"':
                        entre_chaine = True
                    elif c == ouvreur:
                        profondeur += 1
                    elif c == fermeur:
                        profondeur -= 1
                        if profondeur == 0:
                            try:
                                return json.loads(texte[debut : i + 1])
                            except json.JSONDecodeError:
                                break
                i += 1
            debut += 1
    return None


def _normaliser_json(objet: Any) -> list[dict]:
    """Ramène une valeur JSON à une liste de propositions {action,fichier,…}.

    Une liste vide est valide (l'agent n'a aucune modification à proposer).
    """
    candidats: list[Any]
    if isinstance(objet, dict) and "fichier" in objet:
        candidats = [objet]
    elif isinstance(objet, list) and all(isinstance(e, dict) for e in objet):
        candidats = objet
    else:
        raise ErreurAdaptateur(
            "schema_invalide",
            "JSON renvoyé par l'agent : attendu une liste d'objets "
            '{action, fichier, contenu?}, reçu ' + type(objet).__name__ + ".",
        )
    resultats: list[dict] = []
    for item in candidats:
        if not isinstance(item.get("action"), str) and not isinstance(
            item.get("fichier"), str
        ):
            raise ErreurAdaptateur(
                "schema_invalide",
                f"élément JSON invalide : {str(item)[:120]!r}",
            )
        resultats.append(item)
    return resultats


def _protocole_texte(texte: str) -> list[dict]:
    """Parsage du protocole ===FILE/===DELETE=== (blocs dédupliqués)."""
    propositions: list[dict] = []
    for match in RE_DELETE.finditer(texte):
        propositions.append({"action": "delete", "fichier": match.group(1).strip()})
    for match in RE_FICHIER.finditer(texte):
        contenu = match.group(2)
        if contenu.startswith("```"):
            contenu = re.sub(r"^```[^\n]*\n", "", contenu)
            contenu = re.sub(r"\n```\s*$", "", contenu)
        propositions.append(
            {"action": "write", "fichier": match.group(1).strip(), "contenu": contenu}
        )
    uniques: dict[str, dict] = {}
    for prop in propositions:
        uniques[prop["fichier"]] = prop
    return list(uniques.values())


def _valider(propos: list[dict], racine: Path) -> list[dict]:
    """Valide le schéma + sécurité des chemins. Lève ErreurAdaptateur."""
    valides: list[dict] = []
    for prop in propos:
        action = str(prop.get("action", "")).strip()
        if action not in ("patch", "write", "delete"):
            raise ErreurAdaptateur(
                "schema_invalide", f"action inconnue : {action!r}",
                fichier=str(prop.get("fichier") or "") or None,
            )
        fichier = str(prop.get("fichier", "")).strip()
        if not fichier:
            raise ErreurAdaptateur("schema_invalide", "fichier vide dans une proposition.")
        if action == "write":
            if not isinstance(prop.get("contenu"), str):
                raise ErreurAdaptateur(
                    "schema_invalide", "contenu manquant pour un fichier à écrire.",
                    fichier=fichier,
                )
        if action == "patch":
            operations = prop.get("operations")
            if not isinstance(operations, list) or not operations:
                raise ErreurAdaptateur(
                    "schema_invalide",
                    "« operations » doit être une liste non vide pour un patch.",
                    fichier=fichier,
                )
            for op in operations:
                if not isinstance(op, dict) or "ligne" not in op:
                    raise ErreurAdaptateur(
                        "schema_invalide",
                        f"opération de patch invalide (« ligne » obligatoire) : {str(op)[:120]!r}",
                        fichier=fichier,
                    )
        try:
            workspace.chemin_securise(racine, fichier)
        except workspace.CheminHorsProjet as exc:
            raise ErreurAdaptateur(
                "hors_projet", str(exc), fichier=fichier,
            ) from exc
        valides.append(dict(prop, action=action, fichier=fichier))
    return valides


def parser_propositions(texte: str, racine: Path) -> list[dict]:
    """Cascade L1 (JSON) → L2 (= ==FILE===) → L3 (erreur parse_format)."""
    texte = (texte or "").strip()
    if not texte:
        raise ErreurAdaptateur("parse_format", "L'agent n'a rien renvoyé.")

    valeur = _extraire_json_value(texte)
    if valeur is not None:
        return _valider(_normaliser_json(valeur), racine)

    if RE_FICHIER.search(texte) or RE_DELETE.search(texte):
        return _valider(_protocole_texte(texte), racine)

    raise ErreurAdaptateur(
        "parse_format",
        "Sortie de l'agent non reconnaissable : ni JSON ni blocs ===FILE===. "
        f"Début de la sortie : {texte[:120]!r}",
    )


# ─────────────────────────────── Construction du contexte ────────────────────

def decouper_bloc_fichiers(chemins_contexte: list[str], racine: Path) -> str:
    """Compat : délègue à workspace.bloc_fichiers_contexte (lecture seule)."""
    return workspace.bloc_fichiers_contexte(chemins_contexte, racine)


def construire_prompt(
    instruction: str,
    arborescence: str,
    bloc_fichiers: str,
    memoire: str,
    budget_car: int = CONTEXTE_MAX_CAR,
) -> str:
    """Prompt canonique pour le moteur (format strict).

    Le prompt système et la consigne JSON finale sont TOUJOURS gardés ; seuls
    les blocs de contexte (mémoire puis fichiers) portent le poids du plafond
    CONTEXTE_MAX_CAR (~3 800 tokens, sous les 8 000 TPM de Groq).

    `budget_car` est surchargé par le mode EDIT, qui envoie un budget plus large
    : le patch se fait par numéros de ligne, donc la zone à modifier doit être
    réellement présente dans le contexte.
    """
    consigne = (
        "\nConsigne :\n"
        f"{instruction}\n\n"
        "Réponds STRICTEMENT en JSON, sans aucun texte autour, sous la forme :\n"
        '[{"action": "patch", "fichier": "chemin/relatif.ext", "operations": '
        '[{"ligne": 42, "suppression": 1, "ajout": ["ligne de remplacement"]}]}].\n'
        "« ligne » est le numéro de ligne tel qu'il apparaît dans le contexte, il "
        "débute à 1 ; « suppression » vaut le nombre de lignes existantes à "
        "remplacer (1 par défaut) ; « ajout » contient les nouvelles lignes, qui "
        "prennent leur place.\n"
        "PREFERE le patch : il ne demande que les lignes qui changent. N'écris le "
        "fichier COMPLET (\"action\": \"write\") que si le fichier est minuscule, "
        "et supprime un fichier entier avec {\"action\": \"delete\", "
        "\"fichier\": \"chemin/relatif.ext\"}.\n"
        "Ne propose des modifications QUE si c'est nécessaire pour satisfaire "
        "la consigne. Réponds [] si rien ne doit changer."
    )
    systeme = (
        "Tu es l'assistant d'édition d'un IDE. Ne lis et n'écris AUCUN fichier : "
        "réponds uniquement avec le contexte fourni ci-dessous."
    )
    budget = max(
        0, budget_car - len(systeme) - len(consigne) - len(arborescence) - 40
    )
    retenus = _tronquer_prioritaire(
        [
            ("Memoire", f"\nConversation récente (mémoire) :\n{memoire}\n"),
            ("Fichiers", f"\nContenu des fichiers de contexte :\n{bloc_fichiers}\n"),
        ]
        if memoire and bloc_fichiers
        else (
            [("Memoire", f"\nConversation récente (mémoire) :\n{memoire}\n")]
            if memoire
            else [(("Fichiers", f"\nContenu des fichiers de contexte :\n{bloc_fichiers}\n"))]
            if bloc_fichiers
            else []
        ),
        budget,
    )
    prompt = (
        f"{systeme}\n\n"
        f"Arborescence du projet :\n{arborescence}\n"
        + "".join(texte for _, texte in reversed(retenus))
        + consigne
    )
    return prompt


# ─────────────────────────────────── Orchestration des moteurs ───────────────

MoteurAppel = Callable[[str], str]


def proposer_modifications(
    chaine: str,
    sid_opencode: str | None,
    racine: Path,
    pieces: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Appelle les moteurs dans l'ordre OpenCode -> Gemini -> Groq.

    Retourne (moteur, texte_brut). Lève ErreurAdaptateur('moteur_indisponible')
    si tous les moteurs échouent, avec le détail des erreurs.
    """
    engins: list[tuple[str, MoteurAppel]] = []
    if sid_opencode:
        engins.append(
            (
                "opencode",
                lambda c: opencode.envoyer_instruction(str(sid_opencode), racine, c),
            )
        )
    if pieces:
        engins.append(("gemini", lambda c: moteurs.gemini_generer(c, pieces=pieces)))
        engins.append(("groq", lambda c: moteurs.groq_generer(c, pieces=pieces)))
    else:
        engins.append(("gemini", moteurs.gemini_generer))
        engins.append(("groq", moteurs.groq_generer))

    erreurs: list[str] = []
    for nom, appel in engins:
        try:
            brut = appel(chaine)
            if brut:
                return nom, brut
            erreurs.append(f"{nom} : réponse vide.")
        except (opencode.ErreurOpenCode, moteurs.ErreurMoteur) as exc:
            erreurs.append(f"{exc.code} ({nom}) : {exc.message}")
    raise ErreurAdaptateur(
        "moteur_indisponible",
        "Aucun moteur n'a produit de réponse : " + " | ".join(erreurs),
    )
