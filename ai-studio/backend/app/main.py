"""AI Studio — point d'entrée FastAPI (backend).

Lancement : uvicorn app.main:app --host 127.0.0.1 --port 8010
(le port 8000 peut être occupé par un processus système persistant).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import agent, documents, organizer, projets, systeme, terminal
from .config import CORS_ORIGINS, assurer_repertoires

assurer_repertoires()

app = FastAPI(title="AI Studio", version="0.1.0")

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


@app.on_event("shutdown")
def arreter() -> None:
    """Fermeture des terminaux shell au stop du backend."""
    from .services import terminal as terminal_svc

    terminal_svc.arreter_tous()


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-studio-backend"}