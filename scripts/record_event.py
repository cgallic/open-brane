#!/usr/bin/env python3
"""
record_event.py — append one event to events.db.

THIS IS THE ONLY WRITE PATH. All ingestion flows through here.

Usage:
    record_event.py --source <src> --type <type> [--actor <a>] [--ts <iso>] \
        (--payload <json> | --payload-file <path> | --payload-stdin)

Exits 0 on success with JSON {event_id, status, ts, source, type} on stdout.
Exits 1 on error with human-readable message on stderr.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from policy_check import PolicyError, check as policy_check

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))

# strict | sanitize | off. See docs/RECALL-TRACE.md "Unsafe-write filter".
POLICY_MODE = os.environ.get("BRAIN_POLICY_MODE", "strict").lower()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class EventConflict(ValueError):
    """An immutable event identity was retried with different contents."""


def append_event(*, source, event_type, payload, actor=None, ts=None,
                 attachment_uri=None, dedupe_key=None, db_path=None, connection=None):
    """Canonical append API, also used by CRM. Legacy writes remain append-only.

    An optional dedupe key binds source + key to exact canonical content. The
    event and additive dedupe sidecar commit atomically. A supplied connection
    is caller-owned and must be committed/rolled back by that caller.
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    mode = os.environ.get("BRAIN_POLICY_MODE", POLICY_MODE).lower()
    if mode != "off":
        checked = policy_check(source=source, etype=event_type, payload_raw=raw, mode=mode)
        raw = checked[0] if mode == "sanitize" else checked
    if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key.strip()):
        raise ValueError("dedupe_key must be a nonempty string")
    content = {"source": source, "type": event_type, "actor": actor,
               "payload": json.loads(raw), "attachment_uri": attachment_uri}
    if ts is not None:
        content["ts"] = ts
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    own = connection is None
    conn = connection or sqlite3.connect(str(db_path or os.environ.get("BRAIN_DB", DB)), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if own:
            conn.execute("BEGIN IMMEDIATE")
        if dedupe_key is not None:
            conn.execute("CREATE TABLE IF NOT EXISTS event_dedup (source TEXT NOT NULL, "
                         "dedupe_key TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id), "
                         "content_sha256 TEXT NOT NULL, PRIMARY KEY(source,dedupe_key))")
            existing = conn.execute("SELECT event_id,content_sha256 FROM event_dedup "
                                    "WHERE source=? AND dedupe_key=?", (source, dedupe_key)).fetchone()
            if existing:
                if existing[1] != digest:
                    raise EventConflict("dedupe key already identifies different event contents")
                stored = conn.execute("SELECT ts FROM events WHERE id=?", (existing[0],)).fetchone()
                if stored is None:
                    raise EventConflict("dedupe sidecar references a missing event")
                if own:
                    conn.commit()
                return {"event_id": existing[0], "status": "ok", "ts": stored[0],
                        "source": source, "type": event_type, "deduped": True}
        stamp = ts or now_iso()
        cur = conn.execute("INSERT INTO events "
                           "(ts,source,type,actor,payload_json,attachment_uri,ingested_at) "
                           "VALUES (?,?,?,?,?,?,?)", (stamp, source, event_type, actor, raw,
                                                     attachment_uri, now_iso()))
        event_id = int(cur.lastrowid)
        if dedupe_key is not None:
            conn.execute("INSERT INTO event_dedup VALUES (?,?,?,?)",
                         (source, dedupe_key, event_id, digest))
        if own:
            conn.commit()
        result = {"event_id": event_id, "status": "ok", "ts": stamp,
                  "source": source, "type": event_type}
        if dedupe_key is not None:
            result["deduped"] = False
        return result
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        if own:
            conn.close()


def main():
    p = argparse.ArgumentParser(description="Append one event to the brain event log.")
    p.add_argument("--source", required=True, help="e.g. clickup, gdrive, stripe, claude")
    p.add_argument("--type", required=True, dest="etype", help="e.g. task, document, invoice.paid, session")
    p.add_argument("--actor", default=None, help="person/entity + dedup fingerprint")
    p.add_argument("--ts", default=None, help="ISO 8601 UTC (default: now)")
    p.add_argument("--attachment", default=None, help="optional URI pointing at a blob")
    p.add_argument("--dedupe-key", default=None, help="optional immutable retry identity, scoped by source")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--payload", help="inline JSON string")
    g.add_argument("--payload-file", help="path to JSON file")
    g.add_argument("--payload-stdin", action="store_true", help="read JSON from stdin")
    args = p.parse_args()

    if args.payload:
        payload = args.payload
    elif args.payload_file:
        payload = Path(args.payload_file).read_text()
    else:
        payload = sys.stdin.read()

    try:
        json.loads(payload)
    except json.JSONDecodeError as e:
        sys.stderr.write("payload is not valid JSON: {0}\n".format(e))
        sys.exit(1)

    # Policy filter: catches secrets, oversize payloads, code blobs, transcript
    # dumps to wrong source. Set BRAIN_POLICY_MODE=off to bypass (not recommended).
    if POLICY_MODE != "off":
        try:
            if POLICY_MODE == "sanitize":
                payload, warnings = policy_check(
                    source=args.source, etype=args.etype,
                    payload_raw=payload, mode="sanitize",
                )
                for w in warnings:
                    sys.stderr.write("policy warn: {0}\n".format(w))
            else:
                payload = policy_check(
                    source=args.source, etype=args.etype,
                    payload_raw=payload, mode="strict",
                )
        except PolicyError as e:
            sys.stderr.write("policy_check rejected: {0}\n".format(e))
            sys.exit(2)

    try:
        result = append_event(source=args.source, event_type=args.etype, payload=json.loads(payload),
                              actor=args.actor, ts=args.ts, attachment_uri=args.attachment,
                              dedupe_key=args.dedupe_key)
    except (ValueError, PolicyError) as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(2 if isinstance(exc, PolicyError) else 1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
