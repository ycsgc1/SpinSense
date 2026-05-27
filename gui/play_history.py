"""SQLite-backed play history for the dashboard's Recent Plays and the future
History page. Synchronous on purpose — callers wrap individual calls in
asyncio.to_thread() to keep the broadcast loop unblocked.
"""
import os
import sqlite3
import time

DATA_DIR = os.environ.get(
    "SPINSENSE_DATA_DIR",
    os.path.join(os.path.dirname(__file__), ".."),
)
DB_PATH = os.path.join(DATA_DIR, "spinsense.db")


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plays (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              title       TEXT    NOT NULL,
              artist      TEXT    NOT NULL,
              album       TEXT,
              art_url     TEXT,
              art_path    TEXT,
              played_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays (played_at DESC);
            """
        )


def record_play(
    title: str,
    artist: str,
    album: str | None,
    art_url: str | None,
    db_path: str | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO plays (title, artist, album, art_url, played_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, artist, album, art_url, int(time.time())),
        )
        return int(cur.lastrowid)


def set_art_path(play_id: int, art_path: str, db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE plays SET art_path = ? WHERE id = ?",
            (art_path, play_id),
        )


def recent_plays(
    limit: int = 10,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, artist, album, art_url, art_path, played_at "
            "FROM plays ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def count_plays(db_path: str | None = None) -> int:
    with _connect(db_path) as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM plays").fetchone()
        return int(n)
