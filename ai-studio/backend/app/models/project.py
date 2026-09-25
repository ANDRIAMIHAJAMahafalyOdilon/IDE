"""Modèles liés aux projets importés (arborescence)."""

from __future__ import annotations

from pydantic import BaseModel


class RepertoireProjet(BaseModel):
    """Le dossier racine d'un projet importé dans AI Studio."""

    nom: str          # identifiant logique du projet (clé d'accès)
    chemin: str       # chemin absolu du dossier sur disque


class ProjetImport(BaseModel):
    """Requête d'import d'un dossier local."""

    nom: str
    chemin_absolu: str