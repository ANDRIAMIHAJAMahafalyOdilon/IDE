"""Gestion de la base SQLite : historique et statistiques (port).

DB_PATH est patchable par les tests (attribut de module, comme PROJETS_DIR).
Les connexions sont toujours fermées (Windows : suppression du fichier sinon
impossible pendant le nettoyage).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """Retourne une connexion SQLite, crée la base si nécessaire."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Crée les tables nécessaires si elles n'existent pas (idempotent)."""
    conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_path TEXT NOT NULL,
                dst_path TEXT NOT NULL,
                moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_files INTEGER DEFAULT 0,
                total_size INTEGER DEFAULT 0
            );

            INSERT OR IGNORE INTO stats (id, total_files, total_size) VALUES (1, 0, 0);
            """
        )
        conn.commit()
    finally:
        conn.close()


def log_move(src: Path, dst: Path) -> None:
    """Enregistre un déplacement dans la table moves."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO moves (src_path, dst_path) VALUES (?, ?)",
            (str(src), str(dst)),
        )
        conn.commit()
    finally:
        conn.close()


def update_stats(file_delta: int, size_delta: int) -> None:
    """Met à jour les compteurs globaux."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE stats SET total_files = total_files + ?, total_size = total_size + ? "
            "WHERE id = 1",
            (file_delta, size_delta),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_stats() -> tuple[int, int]:
    """Retourne (total_files, total_size)."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT total_files, total_size FROM stats WHERE id = 1").fetchone()
        if row is None:
            return 0, 0
        return row["total_files"], row["total_size"]
    finally:
        conn.close()


def fetch_history(limit: int = 20) -> list[dict]:
    """Derniers déplacements (source, destination, date)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT src_path, dst_path, moved_at FROM moves ORDER BY moved_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()