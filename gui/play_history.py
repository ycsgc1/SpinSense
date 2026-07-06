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


_ENRICHMENT_COLUMNS = {
    "isrc": "TEXT",
    "genre": "TEXT",
    "release_year": "INTEGER",
    # Listening-time / Last.fm-compat columns (2026-07 stats feature):
    "ended_at": "INTEGER",        # unix secs the track stopped; NULL = untracked
    "duration_secs": "INTEGER",   # canonical track length from enrichment
}


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
        existing = {row[1] for row in conn.execute("PRAGMA table_info(plays)")}
        for name, sqltype in _ENRICHMENT_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE plays ADD COLUMN {name} {sqltype}")
        if "deleted_at" not in existing:
            conn.execute("ALTER TABLE plays ADD COLUMN deleted_at INTEGER")


def record_play(
    title: str,
    artist: str,
    album: str | None,
    art_url: str | None,
    db_path: str | None = None,
    *,
    isrc: str | None = None,
    genre: str | None = None,
    release_year: int | None = None,
    duration_secs: int | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO plays "
            "(title, artist, album, art_url, played_at, isrc, genre, release_year, duration_secs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, album, art_url, int(time.time()), isrc, genre,
             release_year, duration_secs),
        )
        return int(cur.lastrowid)


def set_art_path(play_id: int, art_path: str, db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE plays SET art_path = ? WHERE id = ?",
            (art_path, play_id),
        )


def set_ended_at(play_id: int, ended_at: int, db_path: str | None = None) -> None:
    """Stamp when a play stopped. First write wins (ended_at must be NULL) so
    a late duplicate stop-frame can't stretch an already-closed play."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE plays SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (ended_at, play_id),
        )


def delete_play(play_id: int, db_path: str | None = None) -> bool:
    """Soft-delete: stamp deleted_at. Returns True if a live row was hidden."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE plays SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (int(time.time()), play_id),
        )
        return cur.rowcount > 0


def restore_play(play_id: int, db_path: str | None = None) -> bool:
    """Clear deleted_at. Returns True if a soft-deleted row was restored."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE plays SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (play_id,),
        )
        return cur.rowcount > 0


def recent_plays(
    limit: int = 10,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, artist, album, art_url, art_path, played_at, "
            "isrc, genre, release_year "
            "FROM plays WHERE deleted_at IS NULL ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def _unlink_art(data_dir: str, art_path: str) -> None:
    """Unlink a cached art file, but only if it resolves inside data_dir/art.
    Tolerates an already-missing file."""
    full = os.path.normpath(os.path.join(data_dir, art_path))
    art_root = os.path.normpath(os.path.join(data_dir, "art"))
    if not (full == art_root or full.startswith(art_root + os.sep)):
        return
    try:
        os.remove(full)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def purge_deleted(
    grace_seconds: int = 120,
    data_dir: str | None = None,
    db_path: str | None = None,
) -> int:
    """Hard-delete soft-deleted rows whose deleted_at is older than grace_seconds,
    and unlink any art file no longer referenced by a remaining row. Returns the
    number of rows purged."""
    cutoff = int(time.time()) - int(grace_seconds)
    base = data_dir if data_dir is not None else DATA_DIR
    with _connect(db_path) as conn:
        victims = conn.execute(
            "SELECT id, art_path FROM plays "
            "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff,),
        ).fetchall()
        if not victims:
            return 0
        conn.executemany(
            "DELETE FROM plays WHERE id = ?", [(r["id"],) for r in victims]
        )
        # Rows are gone now; unlink art only if nothing remaining references it.
        for art_path in {r["art_path"] for r in victims if r["art_path"]}:
            still = conn.execute(
                "SELECT 1 FROM plays WHERE art_path = ? LIMIT 1", (art_path,)
            ).fetchone()
            if still is None:
                _unlink_art(base, art_path)
    return len(victims)


def count_plays(db_path: str | None = None) -> int:
    with _connect(db_path) as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM plays WHERE deleted_at IS NULL").fetchone()
        return int(n)
