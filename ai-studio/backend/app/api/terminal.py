"""Endpoints du terminal intégré : /api/ws/terminal?projet=…

Flux : connexion -> bannière + prompt, puis les lignes de pwsh ; le client
envoie du texte (commande) sur le même socket. Fermeture du WS = le terminal
du projet survit (réutilisé à la reconnexion).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services import terminal, workspace

router = APIRouter(prefix="/api", tags=["terminal"])


@router.websocket("/ws/terminal")
async def ws_terminal(socket: WebSocket) -> None:
    projet = socket.query_params.get("projet", "")
    try:
        racine = workspace.projet_existant(projet)
    except (ValueError, FileNotFoundError):
        await socket.close(code=4404, reason=f"projet inconnu : {projet}")
        return

    await socket.accept()
    try:
        session = terminal.obtenir_terminal(projet, racine)
    except terminal.TerminalIndisponible as exc:
        await socket.send_text(f"[erreur] {exc}")
        await socket.close(code=4400, reason=str(exc))
        return

    await socket.send_text(session.banniere())

    async def pomper() -> None:
        """Relaye les lignes de pwsh vers le socket."""
        while True:
            ligne = await session.prochaine_ligne()
            if ligne == "":
                await socket.send_text("\n— terminal arrêté —\n")
                return
            await socket.send_text(ligne)

    tache_pompe = asyncio.create_task(pomper())
    try:
        while True:
            texte = await socket.receive_text()
            if not texte:
                continue
            try:
                await session.envoyer(texte)
            except terminal.TerminalIndisponible as exc:
                await socket.send_text(f"[erreur] {exc}")
                break
    except WebSocketDisconnect:
        pass
    finally:
        tache_pompe.cancel()
        try:
            await tache_pompe
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — nettoyage
            pass