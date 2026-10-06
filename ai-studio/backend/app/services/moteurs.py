"""Moteurs LLM de repli (Gemini puis Groq), appelés en dernier recours.

Clients chargés paresseusement : le service démarre même si google-genai ou
groq n'est pas installé, et les erreurs remontent de façon typée (quota,
auth, timeout, indisponible) pour l'événement SSE `erreur`.

INSTRUMENTATION TEMPORAIRE (`logs/uvicorn.log`)
-----------------------------------------------
Chaque flux est tracé : durée totale, délai jusqu'au premier chunk, nombre de
chunks, et en cas d'échec le type d'exception réel (ReadTimeout, 429, …).
Sans ces lignes, impossible de distinguer une coupure « avant le premier
token » d'une coupure « en plein flux », ni de voir un flux qui frôle le délai
d'inactivité. À RETIRER une fois le comportement des moteurs validé.
"""

from __future__ import annotations

import logging
import base64
import re
import time
from typing import Any, Iterator

from ..config import (
    AI_TIMEOUT_MS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
    GROQ_VISION_MODEL,
)

logger = logging.getLogger("ai_studio.moteurs")

# VOLONTAIREMENT pas en INFO/DEBUG : ce projet ne pose AUCUNE configuration de
# logging et la racine d'uvicorn est en WARNING. Passer ces lignes plus bas
# les ferait disparaître d'uvicorn.log sans erreur visible — d'où le rappel
# ci-dessus et le commentaire répété sur chaque appel.
#
# Un flux dont le PREMIER chunk dépasse ce seuil n'est pas en panne : il est
# simplement proche du délai d'inactivité. C'est la donnée qui tranche entre
# « le modèle réfléchit » et « la connexion est mauvaise ». Mettre à 0 pour
# journaliser tous les flux, y compris les rapides.
SEUIL_PREMIER_CHUNK_S = 10.0


class _TraceFlux:
    """Sonde de temps et de volume pour un flux de tokens.

    Ne retient AUCUN texte : uniquement des durées et des compteurs, pour ne
    jamais faire fuiter un extrait de réponse ou de prompt dans les logs.
    """

    __slots__ = ("nom", "debut", "premier_s", "chunks")

    def __init__(self, nom: str) -> None:
        self.nom = nom
        self.debut = time.monotonic()
        self.premier_s: float | None = None
        self.chunks = 0

    def chunk(self) -> None:
        if self.premier_s is None:
            self.premier_s = time.monotonic() - self.debut
        self.chunks += 1

    @property
    def ecoule(self) -> float:
        return time.monotonic() - self.debut

    def echec(self, exc: Exception) -> None:
        # Ni le prompt, ni un chunk, ni str(exc) : le message brut du
        # fournisseur peut reprendre des extraits de la conversation. Le type
        # d'exception suffit à distinguer timeout / quota / réseau.
        logger.error(
            "flux_coupé moteur=%s type=%s ecoule=%.1fs premier_chunk=%s chunks=%d",
            self.nom,
            type(exc).__name__,
            self.ecoule,
            "oui" if self.premier_s is not None else "non",
            self.chunks,
        )

    def reussite(self) -> None:
        lent = self.premier_s is not None and self.premier_s >= SEUIL_PREMIER_CHUNK_S
        if not lent:
            return
        logger.warning(
            "flux_lent moteur=%s premier_chunk=%.1fs total=%.1fs chunks=%d",
            self.nom,
            self.premier_s or 0.0,
            self.ecoule,
            self.chunks,
        )



class ErreurMoteur(Exception):
    """Erreur d'un moteur LLM, avec code normalisé pour l'événement `erreur`."""

    def __init__(self, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _delai_retry(exc: Exception) -> float | None:
    """Lit un Retry-After ou le reset TPM des en-têtes du fournisseur."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    for nom in ("retry-after", "x-ratelimit-reset-tokens"):
        valeur = str(headers.get(nom) or "").strip()
        if not valeur:
            continue
        try:
            return max(0.0, float(valeur))
        except ValueError:
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*s", valeur, re.IGNORECASE)
            if match:
                return float(match.group(1))
    return None


def _classer(exc: Exception, moteur: str) -> ErreurMoteur:
    """Normalise une exception d'API LLM en code SSE (quota/auth/timeout/…).

    `status` peut être numérique (httpx), un drapeau gRPC symbolique
    (« UNAVAILABLE ») ou absent : toute valeur non entière est ignorée et
    traitée comme une indisponibilité générique, jamais comme une erreur
    interne du service.
    """
    texte = f"{exc}".lower()
    statut = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        statut = int(statut)
    except (TypeError, ValueError):
        statut = None
    if statut in (401, 403) or "api key" in texte or "unauthorized" in texte:
        return ErreurMoteur("auth", f"{moteur} : clé API invalide ou non autorisée.")
    # Les statuts HTTP sont prioritaires sur le corps du fournisseur : les
    # erreurs 429 et 413 ont une signification sans ambiguïté.
    if statut == 429:
        return ErreurMoteur(
            "quota", f"{moteur} : quota ou débit par minute dépassé.", _delai_retry(exc)
        )
    if statut == 413:
        return ErreurMoteur(
            "requete_trop_volumineuse",
            f"{moteur} : le corps de la requête dépasse la taille maximale acceptée.",
        )
    if (
        "429" in texte
        or "quota" in texte
        or "rate limit" in texte
        or "tokens per minute" in texte
        or re.search(r"\btpm\b", texte)
    ):
        return ErreurMoteur(
            "quota", f"{moteur} : quota ou débit par minute dépassé.", _delai_retry(exc)
        )
    if (
        "request too large" in texte
        or "request entity too large" in texte
        or "request body too large" in texte
        or "payload too large" in texte
    ):
        return ErreurMoteur(
            "requete_trop_volumineuse",
            f"{moteur} : le corps de la requête dépasse la taille maximale acceptée.",
        )
    if (
        "context length" in texte
        or "context window" in texte
        or "too many tokens" in texte
        or "prompt is too long" in texte
    ):
        return ErreurMoteur(
            "contexte_trop_long",
            f"{moteur} : le prompt dépasse la capacité de contexte du modèle.",
        )
    if statut in (408, 504) or "timeout" in texte or "timed out" in texte:
        return ErreurMoteur("timeout", f"{moteur} : délai dépassé.")
    return ErreurMoteur(
        "moteur_indisponible", f"{moteur} : impossible d'obtenir une réponse ({exc})"
    )


def _octets_partie(partie: dict[str, str]) -> bytes:
    """Décode une partie `file` produite par services.pieces_jointe."""
    url = str(partie.get("url") or "")
    donnees = url.split(",", 1)[1] if url.startswith("data:") and "," in url else url
    return base64.b64decode(donnees, validate=True)


def _contenu_groq(chaine: str, pieces: list[dict[str, str]] | None) -> tuple[Any, bool]:
    """Construit le format OpenAI-compatible attendu par Groq Vision.

    Groq Vision accepte les images via `image_url`. Les PDF restent représentés
    par leur texte extrait dans `chaine` : Gemini/OpenCode reçoivent en plus le
    fichier PDF natif.
    """
    images = [p for p in (pieces or []) if str(p.get("mime") or "").startswith("image/")]
    if not images:
        return chaine, False
    contenu: list[dict[str, Any]] = [{"type": "text", "text": chaine}]
    for partie in images[:3]:  # plafond officiel du modèle vision Groq
        contenu.append(
            {
                "type": "image_url",
                "image_url": {"url": partie["url"]},
            }
        )
    return contenu, True


def _contenus_gemini(chaine: str, pieces: list[dict[str, str]] | None, types: Any) -> list[Any]:
    """Construit un contenu Gemini mêlant texte, images et PDF natifs."""
    contenus: list[Any] = [{"text": chaine}]
    for partie in pieces or []:
        mime = str(partie.get("mime") or "")
        if mime.startswith("image/") or mime == "application/pdf":
            contenus.append(types.Part.from_bytes(data=_octets_partie(partie), mime_type=mime))
    return contenus


def gemini_generer(chaine: str, pieces: list[dict[str, str]] | None = None) -> str:
    """Appelle Gemini avec texte, images et PDF éventuels."""
    if not GEMINI_API_KEY:
        raise ErreurMoteur("auth", "gemini : GEMINI_API_KEY absente du .env.")
    try:
        from google.genai import Client, types
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"gemini : paquet google-genai indisponible ({exc})"
        ) from exc
    client = Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=AI_TIMEOUT_MS))
    trace = _TraceFlux("gemini")
    try:
        reponse = client.models.generate_content(
            model=GEMINI_MODEL, contents=_contenus_gemini(chaine, pieces, types)
        )
        trace.chunk()
        return (reponse.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        trace.echec(exc)
        raise _classer(exc, "gemini") from exc
    finally:
        trace.reussite()


def groq_generer(chaine: str, pieces: list[dict[str, str]] | None = None) -> str:
    """Appelle Groq avec texte et images éventuelles."""
    if not GROQ_API_KEY:
        raise ErreurMoteur("auth", "groq : GROQ_API_KEY absente du .env.")
    try:
        from groq import Groq
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"groq : paquet groq indisponible ({exc})"
        ) from exc
    client = Groq(api_key=GROQ_API_KEY, timeout=AI_TIMEOUT_MS / 1000, max_retries=0)
    trace = _TraceFlux("groq")
    try:
        contenu, vision = _contenu_groq(chaine, pieces)
        completion = client.chat.completions.create(
            model=GROQ_VISION_MODEL if vision else GROQ_MODEL,
            messages=[{"role": "user", "content": contenu}],
        )
        trace.chunk()
        return (completion.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        trace.echec(exc)
        raise _classer(exc, "groq") from exc
    finally:
        trace.reussite()


# ── Variantes STREAMÉES (mode Chat : tokens au fil de l'eau) ─────────────────
# Ce sont des générateurs : l'appel réseau part au premier `next()`. Une
# ErreurMoteur peut donc remonter avant tout token (auth/quota/indispo) ou en
# cours de flux (coupure réseau) — l'appelant décide de la bascule.

def gemini_flux(chaine: str, pieces: list[dict[str, str]] | None = None) -> Iterator[str]:
    """Streame Gemini avec texte, images et PDF éventuels."""
    if not GEMINI_API_KEY:
        raise ErreurMoteur("auth", "gemini : GEMINI_API_KEY absente du .env.")
    try:
        from google.genai import Client, types
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"gemini : paquet google-genai indisponible ({exc})"
        ) from exc
    client = Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=AI_TIMEOUT_MS))
    trace = _TraceFlux("gemini")
    try:
        flux = client.models.generate_content_stream(
            model=GEMINI_MODEL, contents=_contenus_gemini(chaine, pieces, types)
        )
        for bloc in flux:
            texte = bloc.text or ""
            if texte:
                trace.chunk()
                yield texte
    except Exception as exc:  # noqa: BLE001
        trace.echec(exc)
        raise _classer(exc, "gemini") from exc
    trace.reussite()


def groq_flux(chaine: str, pieces: list[dict[str, str]] | None = None) -> Iterator[str]:
    """Streame Groq avec texte et images éventuelles."""
    if not GROQ_API_KEY:
        raise ErreurMoteur("auth", "groq : GROQ_API_KEY absente du .env.")
    try:
        from groq import Groq
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"groq : paquet groq indisponible ({exc})"
        ) from exc
    client = Groq(api_key=GROQ_API_KEY, timeout=AI_TIMEOUT_MS / 1000, max_retries=0)
    trace = _TraceFlux("groq")
    try:
        contenu, vision = _contenu_groq(chaine, pieces)
        flux = client.chat.completions.create(
            model=GROQ_VISION_MODEL if vision else GROQ_MODEL,
            messages=[{"role": "user", "content": contenu}],
            stream=True,
        )
        for chunk in flux:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                trace.chunk()
                yield delta
    except Exception as exc:  # noqa: BLE001
        trace.echec(exc)
        raise _classer(exc, "groq") from exc
    trace.reussite()
