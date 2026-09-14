"""database.py — lightweight SQLite persistence for report history."""

import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "reports.db")


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                constraints TEXT,
                filepath TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def save_report(report_id, topic, constraints, filepath, created_at):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO reports (id, topic, constraints, filepath, created_at) VALUES (?, ?, ?, ?, ?)",
            (report_id, topic, constraints, filepath, created_at),
        )
        conn.commit()


def get_history(limit=20):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, topic, created_at FROM reports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_report(report_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        return dict(row) if row else None
