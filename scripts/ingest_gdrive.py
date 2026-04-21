#!/usr/bin/env python3
"""
ingest_gdrive.py — load a normalized JSONL extract from the Google Drive
extractor into the event log.

The extract file is produced on the machine that has Drive mounted (typically
your laptop) by gdrive_extractor.py, then transferred to wherever events.db
lives. This script reads the extract, emits one event per chunk, and dedupes
by the extract's `hash` field in the actor column (prefixed with "gd-").

Usage:
    ingest_gdrive.py <path-to-extract.jsonl> [--dry-run] [--verbose]

Event shape:
  source = "gdrive"
  type   = "document" (single-chunk) | "document-chunk" (multi-chunk)
  actor  = "gd-{hash}"
  ts     = file mtime ISO 8601
  payload = {
    summary:     f"{folder}/{filename} [1/3]: first 300 chars...",
    content:     chunk text (up to 3800 chars),
    file_path:   relative path from drive root,
    file_name:   filename,
    file_ext:    extension,
    folder:      parent folder,
    chunk_idx:   int,
    chunk_total: int,
    word_count:  int,
    file_size:   int,
  }

This is the canonical adapter pattern. To write a new adapter (ClickUp,
Stripe, Fathom, anything): copy this file, rename the source constant,
rewrite the extract format, keep the rest identical.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BRAIN = Path(os.environ.get("BRAIN_ROOT", Path(__file__).resolve().parent.parent))
DB = Path(os.environ.get("BRAIN_DB", BRAIN / "events.db"))
RECORD = Path(__file__).resolve().parent / "record_event.py"

SOURCE = "gdrive"
ACTOR_PREFIX = "gd-"
SUMMARY_MAX = 300


def load_existing_hashes() -> set[str]:
    if not DB.exists():
        return set()
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(
            "SELECT actor FROM events WHERE source = ? AND actor LIKE ?",
            (SOURCE, f"{ACTOR_PREFIX}%"),
        ).fetchall()
    finally:
        conn.close()
    return {a[0][len(ACTOR_PREFIX):] for a in rows if a[0] and a[0].startswith(ACTOR_PREFIX)}


def record_one(etype: str, ts: str, actor: str, payload: dict) -> dict:
    proc = subprocess.run(
        [
            "python3", str(RECORD),
            "--source", SOURCE,
            "--type", etype,
            "--actor", actor,
            "--ts", ts,
            "--payload-stdin",
        ],
        input=json.dumps(payload),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"status": "error", "error": proc.stderr.strip()}
    return {"status": "ok", "event": json.loads(proc.stdout)}


def main():
    ap = argparse.ArgumentParser(description="Load a Google Drive extract JSONL into events.db.")
    ap.add_argument("extract_path", help="path to extract.jsonl from gdrive_extractor.py")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    path = Path(args.extract_path)
    if not path.exists():
        print(json.dumps({"status": "error", "error": f"extract not found: {path}"}), file=sys.stderr)
        sys.exit(1)

    known = load_existing_hashes()
    result = {"ok": 0, "skipped": 0, "error": 0, "would": 0, "malformed": 0}

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                result["malformed"] += 1
                continue

            h = rec.get("hash") or ""
            if not h:
                result["malformed"] += 1
                continue
            if h in known:
                result["skipped"] += 1
                continue

            content = rec.get("content") or ""
            if not content:
                result["skipped"] += 1
                continue

            file_path = rec.get("file_path") or ""
            file_name = rec.get("file_name") or ""
            folder = rec.get("folder") or ""
            chunk_idx = rec.get("chunk_idx", 0)
            chunk_total = rec.get("chunk_total", 1)
            ts = rec.get("file_mtime_iso") or ""

            if not ts:
                result["malformed"] += 1
                continue

            etype = "document" if chunk_total == 1 else "document-chunk"

            location = f"{folder}/{file_name}" if folder else file_name
            if chunk_total > 1:
                summary_prefix = f"{location} [{chunk_idx+1}/{chunk_total}]: "
            else:
                summary_prefix = f"{location}: "
            summary = (summary_prefix + content)[:SUMMARY_MAX]

            payload = {
                "summary": summary,
                "content": content,
                "file_path": file_path,
                "file_name": file_name,
                "file_ext": rec.get("file_ext", ""),
                "folder": folder,
                "chunk_idx": chunk_idx,
                "chunk_total": chunk_total,
                "word_count": rec.get("word_count", 0),
                "file_size": rec.get("file_size", 0),
            }

            actor = f"{ACTOR_PREFIX}{h}"

            if args.dry_run:
                result["would"] += 1
                if args.verbose:
                    print(f"[would] {location} [{chunk_idx+1}/{chunk_total}] {content[:60]!r}", file=sys.stderr)
                continue

            res = record_one(etype, ts, actor, payload)
            if res["status"] == "ok":
                result["ok"] += 1
                known.add(h)
            else:
                result["error"] += 1
                if args.verbose:
                    print(f"[error] line {lineno}: {res.get('error')}", file=sys.stderr)

    print(json.dumps({"summary": result, "source": SOURCE, "extract": str(path)}, indent=2))


if __name__ == "__main__":
    main()
