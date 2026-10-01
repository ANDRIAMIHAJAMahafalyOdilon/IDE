"""AI Studio — point d'entrée FastAPI (backend).

Lancement : uvicorn app.main:app --host 127.0.0.1 --port 8010
(le port 8000 peut être occupé par un processus système persistant).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import agent, documents, organizer, projets, systeme, terminal
from .config import CORS_ORIGINS, FRONTEND_DIST, assurer_repertoires

assurer_repertoires()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestionnaire de cycle de vie : startup (yield) puis shutdown."""
    # ── Démarrage ──────────────────────────────────────────────────────────
    yield
    # ── Arrêt : fermeture des terminaux shell et du serveur OpenCode ───────
    from .services import opencode as opencode_svc, terminal as terminal_svc

    terminal_svc.arreter_tous()
    opencode_svc.arreter_serveur()


app = FastAPI(title="AI Studio", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent.router)
app.include_router(projets.router)
app.include_router(organizer.router)
app.include_router(systeme.router)
app.include_router(terminal.router)
app.include_router(documents.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-studio-backend"}


# ── Frontend compilé (production) ────────────────────────────────────────────
# Le build Vite est servi par le MÊME service que l'API : le frontend appelle
# `/api/...` en relatif, donc aucun second domaine, aucune entrée CORS et
# aucun préfixe à câbler. Absent en développement (Vite sert la SPA sur 5173 et
# proxifie /api vers ce backend) : les routes API restent alors les seules.
_index = FRONTEND_DIST / "index.html"

if _index.is_file():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{chemin:path}", include_in_schema=False)
    def spa(chemin: str):
        """Fichier du build s'il existe, sinon index.html (routage côté client).

        `/api/...` est volontairement exclu : une URL d'API mal orthographiée
        doit renvoyer un 404 JSON, pas la page d'accueil en HTML. Sans cette
        garde, le frontend recevait du HTML avec un statut 200 et échouait
        plus loin sur un `JSON.parse` incompréhensible.
        """
        if chemin.startswith("api/"):
            raise HTTPException(status_code=404, detail="Route API inconnue.")
        if chemin:
            candidat = (FRONTEND_DIST / chemin).resolve()
            # `is_relative_to` neutralise toute évasion hors de dist/ via `../`.
            if candidat.is_relative_to(FRONTEND_DIST.resolve()) and candidat.is_file():
                return FileResponse(candidat)
        return FileResponse(_index)