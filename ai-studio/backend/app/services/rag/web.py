"""Recherche web DuckDuckGo pour ancrer les réponses du Chat.

Portage de la partie « recherche web » de `etape4_rag_complet.py` (projet
« AI »). Les résultats bruts sont injectés dans le prompt du Chat ; le moteur
est ensuite libre d'en citer les URL exactes.
"""

from __future__ import annotations

from typing import Any


def rechercher(question: str, max_resultats: int = 5) -> list[dict[str, str]]:
    """Interroge DuckDuckGo. Retourne [{titre, url, extrait}, ...].

    Chaque requête ouvre son propre client : pas de session partagée, donc
    pas de fuite d'état entre appels.
    """
    from ddgs import DDGS

    with DDGS() as ddgs:
        resultats = ddgs.text(question, max_results=max_resultats)

    propres = []
    for r in resultats or []:
        propres.append({
            "titre": (r.get("title") or "").strip(),
            "url": (r.get("href") or "").strip(),
            "extrait": (r.get("body") or "").strip(),
        })
    return propres