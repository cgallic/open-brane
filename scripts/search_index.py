#!/usr/bin/env python3
"""
search_index.py — ranked full-text search over the event log, no model needed.

The index is a derived view (SQLite FTS5) stored in the same database file. It
never changes the `events` table and can be dropped and rebuilt at any time:

    search_index.py rebuild        # drop and rebuild from events
    search_index.py status         # indexed vs. total events
    search_index.py search "what did we decide about pricing"

There are no triggers on `events`. Each search first indexes any events newer
than the last indexed id, so every writer (record_event.py, adapters, sqlite3)
is covered and a broken index can never block a write. Events are append-only,
so "id greater than the last indexed id" is the complete set of missing rows.

Matching: words may appear in any order, stemmed ("decisions" finds "decision"),
accents folded, summaries weighted above the rest of the payload. If no event
contains every word, events containing any of the words are returned, best
match first. If that also finds nothing (symbols, partial words), the previous
literal substring search runs. Each result says which of the three was used.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import closing
from pathlib import Path

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))

TABLE = "events_fts"
# Contentless: the index stores terms only, so event text is not duplicated on disk.
CREATE = (f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
          "summary, body, meta, content='', tokenize='porter unicode61 remove_diacritics 2')")
# Index decoded JSON values, not raw JSON: in raw text the escape `\n` glues an
# "n" onto the next word. Payloads that are not valid JSON are indexed as text.
FILL = f"""
INSERT INTO {TABLE}(rowid, summary, body, meta)
SELECT e.id,
  CASE WHEN json_valid(e.payload_json)
       THEN coalesce(json_extract(e.payload_json, '$.summary'), '') ELSE '' END,
  CASE WHEN json_valid(e.payload_json)
       THEN coalesce((SELECT group_concat(j.atom, ' ') FROM json_tree(e.payload_json) j
                      WHERE j.type IN ('text', 'integer', 'real')), '')
       ELSE e.payload_json END,
  e.source || ' ' || e.type || ' ' || coalesce(e.actor, '')
FROM events e WHERE e.id > ? ORDER BY e.id
"""
WEIGHTS = "8.0, 1.0, 2.0"  # summary, body, meta
COLUMNS = "e.id, e.ts, e.source, e.type, e.actor, e.payload_json, e.attachment_uri, e.ingested_at"
SNIPPET_CHARS = 240


def sync(conn):
    """Create the index if needed and add events it has not seen. Returns rows added."""
    def pending():
        last = conn.execute(f"SELECT coalesce(max(rowid), 0) FROM {TABLE}").fetchone()[0]
        newest = conn.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0]
        return last, newest
    try:
        last, newest = pending()
    except sqlite3.OperationalError:
        conn.execute(CREATE)
        last, newest = pending()
    if newest <= last:
        return 0
    # Take the write lock before re-reading, so two searches cannot index a row twice.
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        last, newest = pending()
        if newest > last:
            conn.execute(FILL, (last,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return max(0, newest - last)


def rebuild(conn):
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.commit()
    return sync(conn)


def status(conn):
    total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (TABLE,)).fetchone()
    indexed = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] if exists else 0
    return {"events": total, "indexed": indexed, "index_exists": bool(exists)}


def terms(query):
    """Split a query into FTS5-safe quoted terms. Quoted phrases stay phrases."""
    out = []
    for phrase, word in re.findall(r'"([^"]+)"|(\w+)', query, flags=re.UNICODE):
        words = re.findall(r"\w+", phrase or word, flags=re.UNICODE)
        if words:
            out.append('"' + " ".join(words) + '"')
    return out


def _strings(value):
    """Every text or number inside a decoded payload, in document order."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value)


def _fold(text):
    """Lowercase and drop accents, the way the index does, keeping character positions."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", unicodedata.normalize("NFC", text).lower())
                   if not unicodedata.combining(ch))


def snippet(payload_json, query):
    """A short excerpt around the first query word, so callers can skim results."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        payload = payload_json or ""
    summary = payload.get("summary") if isinstance(payload, dict) else None
    everything = " ".join(_strings(payload))
    # Stems are shorter than words ("decisions" is indexed as "decis"), so look for word starts.
    starts = [_fold(w)[:4] for w in re.findall(r"\w+", query, flags=re.UNICODE)]
    text = everything
    if isinstance(summary, str) and (any(w in _fold(summary) for w in starts)
                                     or not any(w in _fold(everything) for w in starts)):
        text = summary
    text = unicodedata.normalize("NFC", " ".join(text.split()))
    folded = _fold(text)
    hits = [folded.find(w) for w in starts if w in folded]
    start = max(0, (min(hits) if hits else 0) - SNIPPET_CHARS // 3)
    return (("…" if start else "") + text[start:start + SNIPPET_CHARS]
            + ("…" if start + SNIPPET_CHARS < len(text) else ""))


def _row(r, query, match):
    try:
        payload = json.loads(r[5]) if r[5] else None
    except ValueError:
        payload = r[5]
    return {"id": r[0], "ts": r[1], "source": r[2], "type": r[3], "actor": r[4], "payload": payload,
            "attachment_uri": r[6], "ingested_at": r[7], "match": match, "snippet": snippet(r[5], query)}


def _filters(source, etype, since, until, exclude=None):
    where, params = [], []
    for clause, value in (("e.source = ?", source), ("e.type = ?", etype),
                          ("e.ts >= ?", since), ("e.ts <= ?", until)):
        if value:
            where.append(clause)
            params.append(value)
    for skip_source, skip_type in exclude or ():
        where.append("NOT (e.source = ? AND e.type = ?)")
        params.extend((skip_source, skip_type))
    return where, params


def literal(conn, query, source=None, etype=None, since=None, until=None, limit=50, offset=0, exclude=None):
    """Case-insensitive substring search over the stored payload, newest first."""
    where, params = _filters(source, etype, since, until, exclude)
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where.insert(0, "e.payload_json LIKE ? ESCAPE '\\'")
    rows = conn.execute(f"SELECT {COLUMNS} FROM events e WHERE " + " AND ".join(where)
                        + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
                        [f"%{escaped}%", *params, limit, offset]).fetchall()
    return [_row(r, query, "literal") for r in rows]


def search(conn, query, source=None, etype=None, since=None, until=None, limit=50, offset=0, exclude=None):
    """Ranked search with the fallbacks described in the module docstring.

    exclude is an optional list of (source, type) pairs to leave out.
    """
    quoted = terms(query)
    ranked_ok = bool(quoted)
    if ranked_ok:
        try:
            sync(conn)
        except sqlite3.Error:
            # No FTS5/JSON1 in this SQLite build, or a read-only database.
            conn.rollback()
            ranked_ok = False
    if ranked_ok:
        where, params = _filters(source, etype, since, until, exclude)
        sql = (f"SELECT {COLUMNS} FROM {TABLE} f JOIN events e ON e.id = f.rowid "
               f"WHERE {TABLE} MATCH ?" + "".join(" AND " + w for w in where)
               + f" ORDER BY bm25({TABLE}, {WEIGHTS}), e.id DESC LIMIT ? OFFSET ?")
        attempts = [("all_terms", " AND ".join(quoted))]
        if len(quoted) > 1:
            attempts.append(("any_term", " OR ".join(quoted)))
        for match, expression in attempts:
            rows = conn.execute(sql, [expression, *params, limit, offset]).fetchall()
            if rows:
                return [_row(r, query, match) for r in rows]
    return literal(conn, query, source, etype, since, until, limit, offset, exclude)


def main():
    parser = argparse.ArgumentParser(description="Ranked full-text search over the event log.")
    parser.add_argument("command", choices=("search", "rebuild", "status"))
    parser.add_argument("query", nargs="?")
    parser.add_argument("--source")
    parser.add_argument("--type", dest="etype")
    parser.add_argument("--since", help="ISO 8601 lower bound on event time (inclusive)")
    parser.add_argument("--until", help="ISO 8601 upper bound on event time (inclusive)")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    db = Path(os.environ.get("BRAIN_DB", DB))
    if not db.exists():
        sys.stderr.write(f"database not found: {db}\n")
        return 1
    with closing(sqlite3.connect(db, timeout=30)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'events'").fetchone():
            sys.stderr.write(f"no events table in {db}; run brane.py setup first\n")
            return 1
        if args.command == "rebuild":
            print(json.dumps({"status": "ok", "indexed": rebuild(conn)}))
        elif args.command == "status":
            print(json.dumps(status(conn)))
        else:
            if not args.query or not args.query.strip():
                parser.error("search needs a query")
            for row in search(conn, args.query, args.source, args.etype, args.since, args.until, args.limit):
                print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
