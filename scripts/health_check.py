#!/usr/bin/env python3
"""
health_check.py — probe infrastructure and return JSON.

Checks:
- Ollama (LLM inference) at OLLAMA_URL
- Qdrant (vector DB) at QDRANT_URL
- events.db (readable + last event timestamp)

Usage:
    health_check.py [--record]

    --record: write the result as an event (source=health, type=probe)
             so you can query uptime history from events.db itself.
"""
import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BRAIN = Path(os.environ.get("BRAIN_ROOT", Path(__file__).resolve().parent.parent))
DB = Path(os.environ.get("BRAIN_DB", BRAIN / "events.db"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")

HTTP_TARGETS = [
    {"name": "ollama", "url": f"{OLLAMA_URL}/api/tags",         "description": "LLM inference (Ollama)"},
    {"name": "qdrant", "url": f"{QDRANT_URL}/collections",       "description": "vector DB (Qdrant)"},
]


def probe_http(t):
    start = time.time()
    status = None
    body = ""
    err = None
    try:
        req = Request(t["url"], headers={"User-Agent": "brain/health_check"})
        with urlopen(req, timeout=5) as resp:
            status = resp.status
            body = resp.read(2048).decode("utf-8", errors="replace")
    except HTTPError as e:
        status = e.code
        body = str(e)
        err = str(e)
    except URLError as e:
        err = str(e.reason)
    except Exception as e:
        err = str(e)
    latency_ms = int((time.time() - start) * 1000)
    return {
        "name": t["name"],
        "kind": "http",
        "description": t["description"],
        "url": t["url"],
        "status": status,
        "latency_ms": latency_ms,
        "body_snip": body[:200],
        "error": err,
        "healthy": status == 200,
    }


def probe_brain_event_log():
    start = time.time()
    try:
        if not DB.exists():
            return {
                "name": "brain_event_log",
                "kind": "sqlite",
                "description": "events.db",
                "path": str(DB),
                "healthy": False,
                "error": "events.db does not exist",
                "latency_ms": int((time.time() - start) * 1000),
            }
        conn = sqlite3.connect(DB)
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last = conn.execute("SELECT ts, source, type FROM events ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return {
            "name": "brain_event_log",
            "kind": "sqlite",
            "description": "events.db",
            "path": str(DB),
            "healthy": True,
            "total_events": total,
            "last_event_ts": last[0] if last else None,
            "last_event_source": last[1] if last else None,
            "last_event_type": last[2] if last else None,
            "latency_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        return {
            "name": "brain_event_log",
            "kind": "sqlite",
            "description": "events.db",
            "path": str(DB),
            "healthy": False,
            "error": str(e),
            "latency_ms": int((time.time() - start) * 1000),
        }


def main():
    p = argparse.ArgumentParser(description="Probe ingestion infrastructure.")
    p.add_argument("--record", action="store_true", help="write result as a health event")
    args = p.parse_args()

    ts = datetime.now(timezone.utc).isoformat()
    results = [probe_http(t) for t in HTTP_TARGETS]
    results.append(probe_brain_event_log())
    all_healthy = all(r["healthy"] for r in results)
    out = {"ts": ts, "all_healthy": all_healthy, "targets": results}
    print(json.dumps(out, indent=2))

    if args.record:
        import subprocess
        record = Path(__file__).resolve().parent / "record_event.py"
        subprocess.run(
            [
                "python3", str(record),
                "--source", "health",
                "--type", "probe",
                "--ts", ts,
                "--payload-stdin",
            ],
            input=json.dumps(out),
            text=True,
        )


if __name__ == "__main__":
    main()
