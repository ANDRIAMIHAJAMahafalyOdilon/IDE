"""Moteurs LLM de repli (Gemini puis Groq), appelés en dernier recours.

Clients chargés paresseusement : le service démarre même si google-genai ou
groq n'est pas installé, et les erreurs remontent de façon typée (quota,
auth, timeout, indisponible) pour l'événement SSE `erreur`.
"""

from __future__ import annotations

from typing import Iterator

from ..config import AI_TIMEOUT_MS, GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY, GROQ_MODEL


class ErreurMoteur(Exception):
    """Erreur d'un moteur LLM, avec code normalisé pour l'événement `erreur`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
    if (
        statut in (429, 413)
        or "429" in texte
        or "quota" in texte
        or "rate limit" in texte
        or "request too large" in texte
        or "too large" in texte
    ):
        if statut == 413 or "too large" in texte:
            return ErreurMoteur(
                "quota",
                f"{moteur} : le contexte depasse la limite de tokens du modele "
                "(TPM) ; le prompt a ete reduit, reessayez.",
            )
        return ErreurMoteur("quota", f"{moteur} : quota dépassé ou débit limité.")
    if statut in (408, 504) or "timeout" in texte or "timed out" in texte:
        return ErreurMoteur("timeout", f"{moteur} : délai dépassé.")
    return ErreurMoteur(
        "moteur_indisponible", f"{moteur} : impossible d'obtenir une réponse ({exc})"
    )


def gemini_generer(chaine: str) -> str:
    """Appelle Gemini (texte seul). Lève ErreurMoteur."""
    if not GEMINI_API_KEY:
        raise ErreurMoteur("auth", "gemini : GEMINI_API_KEY absente du .env.")
    try:
        from google.genai import Client, types
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"gemini : paquet google-genai indisponible ({exc})"
        ) from exc
    client = Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=AI_TIMEOUT_MS))
    try:
        reponse = client.models.generate_content(
            model=GEMINI_MODEL, contents=[{"text": chaine}]
        )
        return (reponse.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        raise _classer(exc, "gemini") from exc


def groq_generer(chaine: str) -> str:
    """Appelle Groq (texte seul). Lève ErreurMoteur."""
    if not GROQ_API_KEY:
        raise ErreurMoteur("auth", "groq : GROQ_API_KEY absente du .env.")
    try:
        from groq import Groq
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"groq : paquet groq indisponible ({exc})"
        ) from exc
    client = Groq(api_key=GROQ_API_KEY, timeout=AI_TIMEOUT_MS / 1000, max_retries=0)
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL, messages=[{"role": "user", "content": chaine}]
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        raise _classer(exc, "groq") from exc


# ── Variantes STREAMÉES (mode Chat : tokens au fil de l'eau) ─────────────────
# Ce sont des générateurs : l'appel réseau part au premier `next()`. Une
# ErreurMoteur peut donc remonter avant tout token (auth/quota/indispo) ou en
# cours de flux (coupure réseau) — l'appelant décide de la bascule.

def gemini_flux(chaine: str) -> Iterator[str]:
    """Streame la réponse Gemini (texte seul). Lève ErreurMoteur."""
    if not GEMINI_API_KEY:
        raise ErreurMoteur("auth", "gemini : GEMINI_API_KEY absente du .env.")
    try:
        from google.genai import Client, types
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"gemini : paquet google-genai indisponible ({exc})"
        ) from exc
    client = Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=AI_TIMEOUT_MS))
    try:
        flux = client.models.generate_content_stream(
            model=GEMINI_MODEL, contents=[{"text": chaine}]
        )
        for bloc in flux:
            texte = bloc.text or ""
            if texte:
                yield texte
    except Exception as exc:  # noqa: BLE001
        raise _classer(exc, "gemini") from exc


def groq_flux(chaine: str) -> Iterator[str]:
    """Streame la réponse Groq (texte seul). Lève ErreurMoteur."""
    if not GROQ_API_KEY:
        raise ErreurMoteur("auth", "groq : GROQ_API_KEY absente du .env.")
    try:
        from groq import Groq
    except Exception as exc:  # noqa: BLE001
        raise ErreurMoteur(
            "moteur_indisponible", f"groq : paquet groq indisponible ({exc})"
        ) from exc
    client = Groq(api_key=GROQ_API_KEY, timeout=AI_TIMEOUT_MS / 1000, max_retries=0)
    try:
        flux = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": chaine}],
            stream=True,
        )
        for chunk in flux:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta
    except Exception as exc:  # noqa: BLE001
        raise _classer(exc, "groq") from exc