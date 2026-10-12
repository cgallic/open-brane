#!/usr/bin/env python3
"""
semantic_search.py — semantic search over the event log via Qdrant.

Embeds a natural-language query with Ollama's nomic-embed-text model,
searches the `brain_events` Qdrant collection for top-k most similar
points by cosine similarity, and joins the full event payload back from
events.db so callers get everything they need in one round trip.

Usage:
    semantic_search.py --query "when did we decide to use SQLite" [--limit 10] [--source gdrive]

Environment:
    BRAIN_DB          path to events.db
    OLLAMA_URL        default http://127.0.0.1:11434
    QDRANT_URL        default http://127.0.0.1:6333
    EMBED_MODEL       default nomic-embed-text
    BRAIN_COLLECTION  default brain_events

Writes JSONL to stdout — one hit per line:
    {"id":N,"ts":"...","source":"...","type":"...","actor":"...",
     "summary":"...","score":0.78,"payload":{...}}

Exit codes:
    0 — success, zero or more hits written
    1 — infrastructure failure
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import NoReturn

import httpx
from qdrant_client import QdrantClient

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("BRAIN_COLLECTION", "brain_events")
VECTOR_SIZE = int(os.environ.get("EMBED_DIMENSIONS", "768"))


def fail(msg: str, code: int = 1) -> NoReturn:
    print(json.dumps({"status": "error", "error": msg}), file=sys.stderr)
    sys.exit(code)


def embed_query(text: str) -> list[float]:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    vec = data.get("embedding")
    if not isinstance(vec, list) or len(vec) != VECTOR_SIZE:
        raise RuntimeError(
            f"unexpected embedding shape from ollama: len="
            f"{len(vec) if isinstance(vec, list) else 'n/a'}"
        )
    return vec


def fetch_event(conn: sqlite3.Connection, eid: int) -> dict | None:
    row = conn.execute(
        "SELECT id, ts, source, type, actor, payload_json, attachment_uri, ingested_at "
        "FROM events WHERE id = ?",
        (eid,),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[5]) if row[5] else None
    except json.JSONDecodeError:
        payload = None
    return {
        "id": row[0],
        "ts": row[1],
        "source": row[2],
        "type": row[3],
        "actor": row[4],
        "payload": payload,
        "attachment_uri": row[6],
        "ingested_at": row[7],
    }


def main():
    p = argparse.ArgumentParser(description="Semantic search over the brain event log.")
    p.add_argument("--query", required=True, help="natural-language search query")
    p.add_argument("--limit", type=int, default=10, help="max hits to return (default 10)")
    p.add_argument(
        "--source",
        help="optional source filter applied AFTER semantic ranking",
    )
    p.add_argument(
        "--oversample",
        type=int,
        default=3,
        help="fetch N x limit hits from Qdrant before applying the source filter",
    )
    args = p.parse_args()

    if os.environ.get("BRAIN_VECTORS_ENABLED", "true").lower() == "false":
        fail("vector search disabled in deployment config")

    if not DB.exists():
        fail(f"events.db not found at {DB}")

    try:
        vec = embed_query(args.query)
    except Exception as e:
        fail(f"embed query failed: {e}")

    try:
        client = QdrantClient(url=QDRANT_URL, timeout=10)
    except Exception as e:
        fail(f"qdrant unreachable at {QDRANT_URL}: {e}")

    # Fetch `limit * oversample` so that after a source filter we still have
    # enough results. If no source filter, oversample is effectively 1.
    fetch_limit = args.limit * (args.oversample if args.source else 1)

    try:
        hits = client.query_points(
            collection_name=COLLECTION,
            query=vec,
            limit=fetch_limit,
            with_payload=True,
        ).points
    except Exception as e:
        fail(f"qdrant query failed (is the {COLLECTION} collection populated?): {e}")

    conn = sqlite3.connect(DB)
    try:
        written = 0
        for h in hits:
            if written >= args.limit:
                break
            payload = h.payload or {}
            if args.source and payload.get("source") != args.source:
                continue
            try:
                eid = int(h.id)
            except (TypeError, ValueError):
                continue
            event = fetch_event(conn, eid)
            if not event:
                continue
            out = {
                "id": event["id"],
                "ts": event["ts"],
                "source": event["source"],
                "type": event["type"],
                "actor": event["actor"],
                "summary": payload.get("summary"),
                "score": round(float(h.score), 6),
                "payload": event["payload"],
                "attachment_uri": event["attachment_uri"],
            }
            print(json.dumps(out))
            written += 1
    finally:
        conn.close()


if __name__ == "__main__":
    main()
