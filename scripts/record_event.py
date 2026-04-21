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
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def main():
    p = argparse.ArgumentParser(description="Append one event to the brain event log.")
    p.add_argument("--source", required=True, help="e.g. clickup, gdrive, stripe, claude")
    p.add_argument("--type", required=True, dest="etype", help="e.g. task, document, invoice.paid, session")
    p.add_argument("--actor", default=None, help="person/entity + dedup fingerprint")
    p.add_argument("--ts", default=None, help="ISO 8601 UTC (default: now)")
    p.add_argument("--attachment", default=None, help="optional URI pointing at a blob")
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

    ts = args.ts or now_iso()
    ingested_at = now_iso()

    conn = sqlite3.connect(DB)
    try:
        cur = conn.execute(
            "INSERT INTO events (ts, source, type, actor, payload_json, attachment_uri, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, args.source, args.etype, args.actor, payload, args.attachment, ingested_at),
        )
        conn.commit()
        event_id = cur.lastrowid
    finally:
        conn.close()

    print(json.dumps({
        "event_id": event_id,
        "status": "ok",
        "ts": ts,
        "source": args.source,
        "type": args.etype,
    }))


if __name__ == "__main__":
    main()
