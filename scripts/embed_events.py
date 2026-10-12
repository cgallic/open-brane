#!/usr/bin/env python3
"""
embed_events.py — idempotent, incremental semantic embedder over the event log.

Reads every event from events.db, embeds the summary text via Ollama's
nomic-embed-text model, and upserts each vector into a Qdrant collection
named `brain_events`. Re-running is a no-op — only new (unembedded) events
are processed.

Usage:
    embed_events.py [--batch N] [--verbose] [--force]

Environment:
    BRAIN_DB          path to events.db (default: ../events.db relative to script)
    OLLAMA_URL        default http://127.0.0.1:11434
    QDRANT_URL        default http://127.0.0.1:6333
    EMBED_MODEL       default nomic-embed-text
    BRAIN_COLLECTION  default brain_events

Exit codes:
    0 — success (even if zero events were new)
    1 — infrastructure failure (Qdrant unreachable, Ollama unreachable, DB missing)

Writes one JSON object to stdout summarising the run:
    {"status":"ok","embedded":N,"skipped":N,"errors":N,"collection_size":N,
     "latency_ms":N,"collection":"brain_events"}
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import NoReturn

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("BRAIN_COLLECTION", "brain_events")
VECTOR_SIZE = int(os.environ.get("EMBED_DIMENSIONS", "768"))


def fail(msg: str, code: int = 1) -> NoReturn:
    print(json.dumps({"status": "error", "error": msg}), file=sys.stderr)
    sys.exit(code)


def build_embed_text(source: str, etype: str, actor: str | None, payload: dict) -> tuple[str, str]:
    """Return (effective_summary, embed_text).

    effective_summary is stored in the Qdrant payload — a short human-readable
    string. embed_text is sent to the embedding model and includes the
    source/type prefix so the vector carries event-kind context alongside meaning.
    """
    summary = ""
    if isinstance(payload, dict):
        summary = str(payload.get("summary") or "").strip()
    if not summary:
        # Fallback: build a descriptor from payload hints so the summary is at
        # least searchable by file path / url / actor.
        hints = []
        if isinstance(payload, dict):
            for key in ("file", "url", "title", "details", "message", "subject", "path"):
                v = payload.get(key)
                if v:
                    hints.append(f"{key}={v}")
        tail = f"[{actor}]" if actor else ""
        summary = " ".join([f"{source}/{etype}", tail, *hints]).strip()
    embed_text = f"[{source}/{etype}] {summary}"
    return summary, embed_text


def ensure_collection(client: QdrantClient) -> bool:
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION in existing:
        info = client.get_collection(COLLECTION)
        vectors = info.config.params.vectors
        if getattr(vectors, "size", None) != VECTOR_SIZE:
            raise ValueError("collection dimensions differ from EMBED_DIMENSIONS; use a new collection when changing model")
        return False
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    return True


def existing_point_ids(client: QdrantClient) -> set[int]:
    ids: set[int] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            with_payload=False,
            with_vectors=False,
            limit=1000,
            offset=offset,
        )
        for p in points:
            try:
                ids.add(int(p.id))
            except (TypeError, ValueError):
                pass
        if offset is None:
            break
    return ids


def embed(text: str, client: httpx.Client) -> list[float]:
    r = client.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    vec = data.get("embedding")
    if not isinstance(vec, list) or len(vec) != VECTOR_SIZE:
        raise RuntimeError(
            f"unexpected embedding shape from ollama: {type(vec).__name__} "
            f"len={len(vec) if isinstance(vec, list) else 'n/a'}"
        )
    return vec


def main():
    p = argparse.ArgumentParser(description="Embed brain events into Qdrant for semantic search.")
    p.add_argument("--batch", type=int, default=32, help="upsert batch size")
    p.add_argument("--verbose", action="store_true", help="log per-event status to stderr")
    p.add_argument("--force", action="store_true", help="re-embed every event, even if already present")
    args = p.parse_args()

    if os.environ.get("BRAIN_VECTORS_ENABLED", "true").lower() == "false":
        fail("vector indexing disabled in deployment config")

    if not DB.exists():
        fail(f"events.db not found at {DB}")

    try:
        client = QdrantClient(url=QDRANT_URL, timeout=10)
        created = ensure_collection(client)
    except Exception as e:
        fail(f"qdrant unreachable at {QDRANT_URL}: {e}")

    already = set() if args.force else existing_point_ids(client)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            "SELECT id, ts, source, type, actor, payload_json FROM events ORDER BY id"
        ))
    finally:
        conn.close()

    pending = []
    for row in rows:
        eid = int(row["id"])
        if eid in already:
            continue
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except json.JSONDecodeError:
            payload = {}
        summary, text = build_embed_text(
            row["source"], row["type"], row["actor"], payload
        )
        pending.append({
            "id": eid,
            "ts": row["ts"],
            "source": row["source"],
            "type": row["type"],
            "actor": row["actor"],
            "summary": summary,
            "text": text,
        })

    embedded = 0
    errors = 0
    batch: list[PointStruct] = []
    t0 = time.time()

    with httpx.Client() as http:
        for item in pending:
            try:
                vec = embed(item["text"], http)
            except Exception as e:
                errors += 1
                if args.verbose:
                    print(f"embed failed id={item['id']}: {e}", file=sys.stderr)
                continue
            batch.append(PointStruct(
                id=item["id"],
                vector=vec,
                payload={
                    "id": item["id"],
                    "ts": item["ts"],
                    "source": item["source"],
                    "type": item["type"],
                    "actor": item["actor"],
                    "summary": item["summary"],
                },
            ))
            if args.verbose:
                print(f"embedded id={item['id']} ({item['source']}/{item['type']})", file=sys.stderr)
            if len(batch) >= args.batch:
                client.upsert(collection_name=COLLECTION, points=batch)
                embedded += len(batch)
                batch = []
        if batch:
            client.upsert(collection_name=COLLECTION, points=batch)
            embedded += len(batch)

    elapsed_ms = int((time.time() - t0) * 1000)
    try:
        info = client.get_collection(COLLECTION)
        size = info.points_count
    except Exception:
        size = None

    print(json.dumps({
        "status": "ok",
        "embedded": embedded,
        "skipped": len(already),
        "errors": errors,
        "collection_size": size,
        "latency_ms": elapsed_ms,
        "collection": COLLECTION,
        "created_collection": created,
    }))


if __name__ == "__main__":
    main()
