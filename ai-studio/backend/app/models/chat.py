"""Modèles des échanges avec l'agent et de l'application des modifications.

Ces modèles reflètent exactement le JSON produit par services.diff
(as_dict / from_dict) : le frontend ne manipule que des hunks, jamais de
contenu brut à comparer lui-même (décision d'architecture : build_diff est
appelé côté backend uniquement).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Hunk(BaseModel):
    """Un bloc de modification autonome (conventions `diff unifié`)."""

    id: int
    old_start: int
    old_end: int
    old_count: int
    new_start: int
    new_end: int
    new_count: int
    lignes: list[dict[str, Any]] = Field(
        ..., description="lignes {type: 'context'|'add'|'del', contenu, old_no, new_no}"
    )


class Proposition(BaseModel):
    """Proposition de modification d'un fichier, tel qu'émis en SSE."""

    fichier: str
    action: Literal["write", "delete"] = "write"
    source_hash: str
    hunks: list[Hunk]
    stats: dict[str, int]


class ChangementApplique(BaseModel):
    """Un fichier à modifier via /api/agent/apply-changes."""

    fichier: str
    action: Literal["write", "delete"] = "write"
    source_hash: str | None = None
    acceptes: list[Hunk] = Field(default_factory=list, description="hunks validés par l'utilisateur")


class RequeteApply(BaseModel):
    """Corps de POST /api/agent/apply-changes."""

    projet: str
    session: str | None = None
    modifications: list[ChangementApplique]


class ResultatFichier(BaseModel):
    """Résultat d'application pour un fichier."""

    fichier: str
    statut: Literal["ok", "erreur", "pas_modifie"]
    message: str | None = None
    nouveau_sha: str | None = None


class ReqChatContext(BaseModel):
    """Contexte transmis en SSE pour l'événement `debut`."""

    session: str | None = None
    autoris: bool = False
    moteur: str


class ErreurSSE(BaseModel):
    """Payload de l'événement SSE `erreur` (codes normalisés)."""

    code: str
    message: str
    fichier: str | None = None


class PieceJoine(BaseModel):
    """Pièce jointe envoyée avec un message (mode chat).

    `donnees` est le contenu du fichier en base64, SANS préfixe `data:`. Le
    frontend n'envoie jamais un chemin : le backend ne lit que ce qu'il reçoit,
    donc aucun chemin arbitraire du disque utilisateur n'est exposé ici.
    """

    nom: str
    mime: str = ""
    donnees: str


class RequeteChat(BaseModel):
    """Corps de POST /api/agent/chat (streaming SSE).

    `mode="edit"` (défaut) : l'agent propose des hunks (projet requis).
    `mode="chat"` : discussion libre streamée (projet optionnel, lecture seule,
    aucune modification de fichier — jamais de `proposition`).

    `pieces` n'est honoré qu'en mode `chat` : ce sont les images et PDF choisis
    avec le bouton « + » du composeur. Le mode edit n'a pas de pièce jointe.
    """

    pieces: list[PieceJoine] = Field(default_factory=list)

    mode: Literal["chat", "edit"] = "edit"
    projet: str | None = None
    message: str
    session: str | None = None
    autoriser_modifications: bool = False
    # RAG (mode chat) : `documents` recherche dans les cours indexés et
    # `web` interroge DuckDuckGo pour ancrer la réponse (sources citées).
    documents: bool = False
    web: bool = False
    # Chemins des fichiers de contexte (fichier ouvert dans l'éditeur, fichiers
    # cochés). Le CONTENU est lu côté backend — jamais envoyé par le frontend.
    fichiers_contexte: list[str] | None = None
    # Note dev : liste de {"chemin", "contenu"} permettant de tester le flux
    # complet (build_diff -> hunks -> SSE) sans dépendre du moteur réel.
    simulation: list[FichierSimule] | None = None


class RequeteTacheControle(BaseModel):
    """Commande de contrôle d’une tâche OpenCode par projet."""

    projet: str


class RequetePermission(BaseModel):
    """Décision utilisateur transmise à une permission OpenCode réelle."""

    projet: str
    request_id: str
    reply: Literal["once", "always", "reject"]
    message: str | None = None


class RequeteTache(BaseModel):
    """Corps de POST /api/agent/tache (mode autonome — l'agent exécute réellement)."""

    projet: str
    message: str
    session: str | None = None


class FichierSimule(BaseModel):
    """Fichier proposé par le moteur de simulation."""

    chemin: str
    contenu: str