#!/usr/bin/env python3
"""
ingest_claude.py — scan Claude Code sessions (JSONL files) and record each as
an event.

Claude Code writes session transcripts to ~/.claude/projects/<project>/*.jsonl
(or on recent versions, ~/.claude/sessions/). Each line is a message. We fold
each session file into ONE event (source=claude, type=session) whose payload
summarises the session and includes the full transcript chunks.

Usage:
    ingest_claude.py [--root PATH] [--since N_DAYS] [--verbose]

Dedup: actor = "cc-{sha256(file_path)[:16]}-{num_lines}". If the file grows,
the actor changes and a new event is recorded. Re-running without new lines
is a no-op.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RECORD = SCRIPT_DIR / "record_event.py"

DEFAULT_ROOT_CANDIDATES = [
    Path.home() / ".claude" / "projects",
    Path.home() / ".claude" / "sessions",
]


def session_fingerprint(path: Path, num_lines: int) -> str:
    key = f"{path.resolve()}:{num_lines}".encode("utf-8", errors="replace")
    return "cc-" + hashlib.sha256(key).hexdigest()[:16]


def summarise_session(path: Path, lines: list[str]) -> dict:
    first_user_msg = ""
    last_ts = ""
    first_ts = ""
    msg_count = 0
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg_count += 1
        ts = obj.get("timestamp") or obj.get("ts")
        if ts and not first_ts:
            first_ts = ts
        if ts:
            last_ts = ts
        if not first_user_msg and obj.get("role") == "user":
            content = obj.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            first_user_msg = str(content)[:200]
    return {
        "file": str(path),
        "project": path.parent.name,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "message_count": msg_count,
        "first_user_message": first_user_msg,
        "summary": f"Claude session {path.parent.name}/{path.name}: {first_user_msg[:120]}",
    }


def record(source_ts: str, actor: str, payload: dict) -> bool:
    proc = subprocess.run(
        ["python3", str(RECORD),
         "--source", "claude", "--type", "session",
         "--actor", actor, "--ts", source_ts, "--payload-stdin"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="Ingest Claude Code session transcripts.")
    ap.add_argument("--root", default=None, help="override session root dir")
    ap.add_argument("--since", type=int, default=30, help="ingest files modified within N days (default 30)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    roots = [Path(args.root)] if args.root else [p for p in DEFAULT_ROOT_CANDIDATES if p.exists()]
    if not roots:
        print(json.dumps({"status": "error", "error": "no Claude sessions directory found"}))
        sys.exit(1)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.since)).timestamp()
    ok = skipped = errors = 0

    for root in roots:
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    skipped += 1
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                errors += 1
                continue
            if not lines:
                continue
            actor = session_fingerprint(path, len(lines))
            payload = summarise_session(path, lines)
            ts = payload.get("last_ts") or datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            if record(ts, actor, payload):
                ok += 1
                if args.verbose:
                    print(f"[ok] {path}", file=sys.stderr)
            else:
                errors += 1

    print(json.dumps({"status": "ok", "ingested": ok, "skipped": skipped, "errors": errors}))


if __name__ == "__main__":
    main()
