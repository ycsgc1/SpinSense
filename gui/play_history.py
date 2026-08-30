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
    # Album/edition reconciliation (2026-07): 1 = album set manually, never
    # auto-rewritten. NULL/0 = auto-managed.
    "album_locked": "INTEGER",
    # Play clock (2026-08, core/track_clock.py). `played_at` deliberately keeps
    # its old meaning ("when we identified it") so every existing row, every
    # stats query and history ordering stay valid; the true track start lands
    # here instead. NULL on both = unknown, which is every pre-feature row.
    "started_at": "INTEGER",          # unix secs the track began on the platter
    "join_offset_secs": "INTEGER",    # secs into the track when we started hearing it
    # Edition evidence (2026-08). 1 = every edition this track appears on
    # carries a qualifier, so the record played must be that edition and the
    # whole run can be upgraded to it. NULL/0 = the base album exists, which
    # proves nothing. See spinsense.albums.choose_edition().
    "album_exclusive": "INTEGER",
    # Last.fm (2026-08). Non-NULL = this play has been submitted and must never
    # be submitted again; the value is when we sent it. Rows Last.fm ignored or
    # that aged out are stamped too — retrying either forever would be a leak.
    "scrobbled_at": "INTEGER",
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
    started_at: int | None = None,
    join_offset_secs: int | None = None,
    album_exclusive: bool | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO plays "
            "(title, artist, album, art_url, played_at, isrc, genre, release_year, "
            "duration_secs, started_at, join_offset_secs, album_exclusive) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, album, art_url, int(time.time()), isrc, genre,
             release_year, duration_secs, started_at, join_offset_secs,
             1 if album_exclusive else 0),
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


def get_play(play_id: int, db_path: str | None = None) -> dict | None:
    """One live (non-deleted) play row as a dict, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM plays WHERE id = ? AND deleted_at IS NULL", (play_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def set_album(play_id: int, album: str, locked: bool = True,
              db_path: str | None = None) -> bool:
    """Set a play's album. `locked` marks it manually-set so auto
    reconciliation leaves it alone. Returns True if a live row changed."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE plays SET album = ?, album_locked = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (album, 1 if locked else 0, play_id),
        )
        return cur.rowcount > 0


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
            "isrc, genre, release_year, duration_secs, ended_at, album_locked, "
            "started_at, join_offset_secs, album_exclusive "
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


def mark_scrobbled(play_ids, scrobbled_at: int, db_path: str | None = None) -> int:
    """Stamp plays as submitted. First write wins, so a retry that races a
    successful submission can't move the timestamp. Returns rows changed."""
    ids = list(play_ids)
    if not ids:
        return 0
    with _connect(db_path) as conn:
        cur = conn.executemany(
            "UPDATE plays SET scrobbled_at = ? WHERE id = ? AND scrobbled_at IS NULL",
            [(int(scrobbled_at), int(i)) for i in ids],
        )
        return cur.rowcount


def count_plays(db_path: str | None = None) -> int:
    with _connect(db_path) as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM plays WHERE deleted_at IS NULL").fetchone()
        return int(n)


# --- Scrobble ledger -------------------------------------------------------
# Last.fm's published track.scrobble eligibility rule. A future scrobbler is a
# pure consumer of these: auth + POST, no further schema or engine work.
SCROBBLE_MIN_DURATION_SECS = 30    # tracks shorter than this never scrobble
SCROBBLE_ABSOLUTE_SECS = 240       # 4 minutes always qualifies


def scrobble_listened_secs(row: dict) -> int | None:
    """Seconds of this play we actually heard, or None if unknowable.

    `played_at` is when we identified the track, so it already excludes
    whatever ran before we joined; `join_offset_secs` only matters for the
    total, not the heard time. An unclosed play (no `ended_at` — the GUI
    restarted mid-track) is never estimated, by the same rule stats.py uses.
    """
    ended_at = row.get("ended_at")
    played_at = row.get("played_at")
    if ended_at is None or played_at is None:
        return None
    return max(0, int(ended_at) - int(played_at))


def scrobble_eligible(row: dict) -> bool:
    """Last.fm's rule: the track must be longer than 30 s, and must have been
    played for at least half its length or 4 minutes, whichever comes first."""
    duration = row.get("duration_secs")
    listened = scrobble_listened_secs(row)
    if not duration or listened is None:
        return False
    if int(duration) <= SCROBBLE_MIN_DURATION_SECS:
        return False
    return listened >= min(int(duration) / 2, SCROBBLE_ABSOLUTE_SECS)


# A run of the same record never spans this; used to scope "when did this album
# finish" to one listening session rather than every time it was ever played.
_ALBUM_SESSION_WINDOW_SECS = 86400


def album_last_ended(artist: str, album: str | None, near_played_at: int,
                     db_path: str | None = None) -> int | None:
    """When this album last finished playing, around the given time.

    A side is played as a unit, so "the album is over" is the useful moment to
    act on — not the end of each individual track. Scoped to a day around the
    play so that putting the same record on next week doesn't hold last week's
    plays hostage.

    None when the album is unknown or nothing has closed yet.
    """
    if not album:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(ended_at) FROM plays "
            "WHERE deleted_at IS NULL AND ended_at IS NOT NULL "
            "AND artist = ? AND album = ? AND played_at BETWEEN ? AND ?",
            (artist, album,
             int(near_played_at) - _ALBUM_SESSION_WINDOW_SECS,
             int(near_played_at) + _ALBUM_SESSION_WINDOW_SECS),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None


UNKNOWN_ALBUM = "Unknown Album"


def album_for_track(artist: str, title: str,
                    db_path: str | None = None) -> str | None:
    """The album this exact track was last filed under, or None.

    A vinyl collection is small and repetitive: most people own one or two
    pressings of any given record, and the same song identified today almost
    certainly belongs to the same album it belonged to last time. That makes
    the listener's own history a better oracle than a relevance-ranked search —
    it is a record of what they actually own. If they have only ever played the
    deluxe, the deluxe is the right answer for them.

    An album the listener set by hand wins over one we guessed, however recent
    the guess: `album_locked` is them telling us, and that outranks inference.
    """
    if not artist or not title:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT album FROM plays "
            "WHERE deleted_at IS NULL AND artist = ? AND title = ? "
            "AND album IS NOT NULL AND album != ? "
            "ORDER BY COALESCE(album_locked, 0) DESC, played_at DESC LIMIT 1",
            (artist, title, UNKNOWN_ALBUM),
        ).fetchone()
        return row["album"] if row is not None else None


def scrobble_candidates(
    since: int = 0,
    limit: int = 200,
    db_path: str | None = None,
    *,
    pending_only: bool = False,
) -> list[dict]:
    """Closed plays since `since`, oldest first, with the scrobble maths applied.

    Oldest-first because Last.fm expects batches in chronological order. Each
    row carries `timestamp` (the true track start where we know it, else the
    identification time — what track.scrobble wants), plus `listened_secs` and
    `eligible` so the caller decides nothing.

    `pending_only` narrows to rows not yet submitted — what the scrobbler drains.
    Without it this is the general ledger view, useful for inspecting history.
    """
    limit = max(1, min(int(limit), 1000))
    unsent = " AND scrobbled_at IS NULL" if pending_only else ""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, artist, album, played_at, ended_at, duration_secs, "
            "started_at, join_offset_secs, scrobbled_at "
            "FROM plays WHERE deleted_at IS NULL AND ended_at IS NOT NULL "
            f"AND played_at >= ?{unsent} ORDER BY played_at ASC, id ASC LIMIT ?",
            (int(since), limit),
        ).fetchall()

    out = []
    for r in rows:
        row = dict(r)
        row["timestamp"] = row["started_at"] or row["played_at"]
        row["listened_secs"] = scrobble_listened_secs(row)
        row["eligible"] = scrobble_eligible(row)
        out.append(row)
    return out
