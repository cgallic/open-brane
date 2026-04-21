#!/usr/bin/env python3
"""
query_events.py — filtered query over the event log, returns JSONL.

Usage:
    query_events.py [--source X] [--type X] [--actor X] [--since ISO] [--until ISO] [--limit N] [--order asc|desc]

Writes one JSON object per event, one per line, to stdout.
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))


def main():
    p = argparse.ArgumentParser(description="Query the brain event log.")
    p.add_argument("--source")
    p.add_argument("--type", dest="etype")
    p.add_argument("--actor")
    p.add_argument("--since", help="ISO 8601 lower bound (inclusive)")
    p.add_argument("--until", help="ISO 8601 upper bound (inclusive)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--order", choices=["asc", "desc"], default="desc")
    args = p.parse_args()

    where, params = [], []
    if args.source:
        where.append("source = ?")
        params.append(args.source)
    if args.etype:
        where.append("type = ?")
        params.append(args.etype)
    if args.actor:
        where.append("actor = ?")
        params.append(args.actor)
    if args.since:
        where.append("ts >= ?")
        params.append(args.since)
    if args.until:
        where.append("ts <= ?")
        params.append(args.until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = args.order.upper()
    sql = (
        "SELECT id, ts, source, type, actor, payload_json, attachment_uri, ingested_at "
        "FROM events" + clause + " ORDER BY ts " + order + " LIMIT ?"
    )
    params.append(args.limit)

    conn = sqlite3.connect(DB)
    try:
        for row in conn.execute(sql, params):
            out = {
                "id": row[0],
                "ts": row[1],
                "source": row[2],
                "type": row[3],
                "actor": row[4],
                "payload": json.loads(row[5]) if row[5] else None,
                "attachment_uri": row[6],
                "ingested_at": row[7],
            }
            print(json.dumps(out))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
