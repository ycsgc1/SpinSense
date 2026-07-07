"""Aggregation queries for the /stats page ("Wrapped"). Synchronous on
purpose — backend_main wraps calls in asyncio.to_thread(), same contract as
play_history.py. Period boundaries and chart buckets use the server's local
timezone. Listening time counts only rows with a real ended_at (no
estimation for pre-feature plays — by design)."""
import datetime

from play_history import _connect

LISTEN_CAP_SECS = 2400  # 40 min: guards clock skew / missed stop frames
TOP_N = 5

_WHERE = "deleted_at IS NULL AND played_at >= ? AND played_at < ?"


def _period_bounds(period: str, year: int | None, month: int | None,
                   now: int | None = None) -> tuple[int, int]:
    """(start, end) unix seconds in server-local time; end is exclusive.
    'all' -> (0, just past now). Raises ValueError on bad period/month."""
    if month is not None and not 1 <= month <= 12:
        raise ValueError(f"invalid month: {month!r}")
    now_dt = (datetime.datetime.fromtimestamp(now) if now is not None
              else datetime.datetime.now())
    if period == "all":
        return 0, int(now_dt.timestamp()) + 1
    if period == "year":
        y = year if year is not None else now_dt.year
        return (int(datetime.datetime(y, 1, 1).timestamp()),
                int(datetime.datetime(y + 1, 1, 1).timestamp()))
    if period == "month":
        y = year if year is not None else now_dt.year
        m = month if month is not None else now_dt.month
        start = datetime.datetime(y, m, 1)
        end = (datetime.datetime(y + 1, 1, 1) if m == 12
               else datetime.datetime(y, m + 1, 1))
        return int(start.timestamp()), int(end.timestamp())
    raise ValueError(f"invalid period: {period!r}")


def _totals(conn, start, end) -> dict:
    plays, artists = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT artist) FROM plays WHERE {_WHERE}",
        (start, end)).fetchone()
    (tracks,) = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM plays WHERE {_WHERE}"
        " GROUP BY title, artist)", (start, end)).fetchone()
    secs, tracked = conn.execute(
        "SELECT COALESCE(SUM(MAX(0, MIN(ended_at - played_at, ?))), 0), COUNT(*)"
        f" FROM plays WHERE {_WHERE} AND ended_at IS NOT NULL",
        (LISTEN_CAP_SECS, start, end)).fetchone()
    return {"plays": plays, "unique_tracks": tracks, "unique_artists": artists,
            "listening_secs": int(secs), "listening_tracked_plays": tracked}


def _latest_art_subquery(match: str) -> str:
    return (f"(SELECT art_path FROM plays p2 WHERE {match}"
            " AND p2.deleted_at IS NULL AND p2.played_at >= ? AND p2.played_at < ?"
            " AND p2.art_path IS NOT NULL ORDER BY p2.played_at DESC, p2.id DESC"
            " LIMIT 1)")


def _top_artists(conn, start, end) -> list[dict]:
    art = _latest_art_subquery("p2.artist = p.artist")
    rows = conn.execute(
        f"SELECT p.artist, COUNT(*) AS plays, {art} AS art_path"
        f" FROM plays p WHERE {_WHERE}"
        " GROUP BY p.artist ORDER BY plays DESC, p.artist ASC LIMIT ?",
        (start, end, start, end, TOP_N)).fetchall()
    return [dict(r) for r in rows]


def _top_tracks(conn, start, end) -> list[dict]:
    art = _latest_art_subquery("p2.title = p.title AND p2.artist = p.artist")
    rows = conn.execute(
        f"SELECT p.title, p.artist, COUNT(*) AS plays, {art} AS art_path"
        f" FROM plays p WHERE {_WHERE}"
        " GROUP BY p.title, p.artist"
        " ORDER BY plays DESC, p.artist ASC, p.title ASC LIMIT ?",
        (start, end, start, end, TOP_N)).fetchall()
    return [dict(r) for r in rows]


def _top_albums(conn, start, end, total) -> dict:
    art = _latest_art_subquery("p2.album = p.album AND p2.artist = p.artist")
    where_album = "p.album IS NOT NULL AND p.album != 'Unknown Album'"
    rows = conn.execute(
        f"SELECT p.album, p.artist, COUNT(*) AS plays, {art} AS art_path"
        f" FROM plays p WHERE {_WHERE} AND {where_album}"
        " GROUP BY p.album, p.artist"
        " ORDER BY plays DESC, p.album ASC, p.artist ASC LIMIT ?",
        (start, end, start, end, TOP_N)).fetchall()
    (covered,) = conn.execute(
        f"SELECT COUNT(*) FROM plays WHERE {_WHERE}"
        " AND album IS NOT NULL AND album != 'Unknown Album'",
        (start, end)).fetchone()
    return {"covered": covered, "total": total, "top": [dict(r) for r in rows]}


def _bucket_starts(start_dt, last_dt, bucket):
    """All bucket keys from start_dt through last_dt inclusive."""
    keys = []
    cur = start_dt
    while cur <= last_dt:
        if bucket == "day":
            keys.append(cur.strftime("%Y-%m-%d"))
            cur = cur + datetime.timedelta(days=1)
        else:
            keys.append(cur.strftime("%Y-%m"))
            cur = (datetime.datetime(cur.year + 1, 1, 1) if cur.month == 12
                   else datetime.datetime(cur.year, cur.month + 1, 1))
    return keys


def _plays_over_time(conn, period, start, end, now_secs) -> dict:
    bucket = "day" if period == "month" else "month"
    fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m"
    counts = dict(conn.execute(
        f"SELECT strftime('{fmt}', played_at, 'unixepoch', 'localtime') AS k,"
        f" COUNT(*) FROM plays WHERE {_WHERE} GROUP BY k",
        (start, end)).fetchall())

    if period == "all":
        (first,) = conn.execute(
            "SELECT MIN(played_at) FROM plays WHERE deleted_at IS NULL"
        ).fetchone()
        if first is None:
            return {"bucket": bucket, "buckets": []}
        start_dt = datetime.datetime.fromtimestamp(first)
    else:
        start_dt = datetime.datetime.fromtimestamp(start)

    # Clamp the zero-filled range at "now" so a current period doesn't chart
    # the future; past periods run to their real end.
    last = min(end - 1, now_secs)
    last_dt = datetime.datetime.fromtimestamp(last)
    if bucket == "day":
        start_dt = datetime.datetime(start_dt.year, start_dt.month, start_dt.day)
        last_dt = datetime.datetime(last_dt.year, last_dt.month, last_dt.day)
    else:
        start_dt = datetime.datetime(start_dt.year, start_dt.month, 1)
        last_dt = datetime.datetime(last_dt.year, last_dt.month, 1)

    keys = _bucket_starts(start_dt, last_dt, bucket)
    return {"bucket": bucket,
            "buckets": [{"key": k, "plays": counts.get(k, 0)} for k in keys]}


def _genres(conn, start, end, total) -> dict:
    rows = conn.execute(
        f"SELECT genre, COUNT(*) AS plays FROM plays WHERE {_WHERE}"
        " AND genre IS NOT NULL GROUP BY genre"
        " ORDER BY plays DESC, genre ASC LIMIT ?",
        (start, end, TOP_N)).fetchall()
    (covered,) = conn.execute(
        f"SELECT COUNT(*) FROM plays WHERE {_WHERE} AND genre IS NOT NULL",
        (start, end)).fetchone()
    return {"covered": covered, "total": total,
            "top": [{"genre": r["genre"], "plays": r["plays"]} for r in rows]}


def _decades(conn, start, end, total) -> dict:
    rows = conn.execute(
        f"SELECT (release_year / 10) * 10 AS decade, COUNT(*) AS plays"
        f" FROM plays WHERE {_WHERE} AND release_year IS NOT NULL"
        " GROUP BY decade ORDER BY decade ASC",
        (start, end)).fetchall()
    (covered,) = conn.execute(
        f"SELECT COUNT(*) FROM plays WHERE {_WHERE} AND release_year IS NOT NULL",
        (start, end)).fetchone()
    return {"covered": covered, "total": total,
            "buckets": [{"decade": r["decade"], "plays": r["plays"]} for r in rows]}


def compute_stats(period: str, year: int | None = None, month: int | None = None,
                  db_path: str | None = None, now: int | None = None) -> dict:
    now_secs = now if now is not None else int(datetime.datetime.now().timestamp())
    start, end = _period_bounds(period, year, month, now=now_secs)
    now_dt = datetime.datetime.fromtimestamp(now_secs)
    resolved_year = None
    resolved_month = None
    if period in ("year", "month"):
        resolved_year = year if year is not None else now_dt.year
        if period == "month":
            resolved_month = month if month is not None else now_dt.month
    with _connect(db_path) as conn:
        totals = _totals(conn, start, end)
        return {
            "period": {"kind": period, "year": resolved_year,
                        "month": resolved_month, "start": start, "end": end},
            "totals": totals,
            "top_artists": _top_artists(conn, start, end),
            "top_tracks": _top_tracks(conn, start, end),
            "top_albums": _top_albums(conn, start, end, totals["plays"]),
            "plays_over_time": _plays_over_time(conn, period, start, end, now_secs),
            "genres": _genres(conn, start, end, totals["plays"]),
            "decades": _decades(conn, start, end, totals["plays"]),
        }
