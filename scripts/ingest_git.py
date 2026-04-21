#!/usr/bin/env python3
"""
ingest_git.py — record git commits from configured repos as events.

Reads config/git-repos.json:
    {
      "repos": [
        {"path": "/var/www/app1", "name": "app1"},
        {"path": "/var/www/app2", "name": "app2"}
      ]
    }

For each repo, runs `git log --since="<N days>" --pretty=format:...` and
records each commit as one event (source=git, type=commit). Dedup via
actor = "<repo-name>:<short-sha>".

Usage:
    ingest_git.py [--since N_DAYS] [--config PATH] [--verbose]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RECORD = SCRIPT_DIR / "record_event.py"
CONFIG_DEFAULT = SCRIPT_DIR.parent / "config" / "git-repos.json"


def git_log(repo_path: Path, since_days: int) -> list[dict]:
    """Return list of commits from the repo in the last N days."""
    fmt = "%H%x1f%h%x1f%aI%x1f%an%x1f%ae%x1f%s%x1f%b%x1e"
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "log",
             f"--since={since_days} days ago",
             f"--pretty=format:{fmt}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []

    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 7:
            continue
        commits.append({
            "sha": parts[0],
            "short_sha": parts[1],
            "ts": parts[2],
            "author_name": parts[3],
            "author_email": parts[4],
            "subject": parts[5],
            "body": parts[6].strip(),
        })
    return commits


def record(ts: str, actor: str, payload: dict) -> bool:
    proc = subprocess.run(
        ["python3", str(RECORD),
         "--source", "git", "--type", "commit",
         "--actor", actor, "--ts", ts, "--payload-stdin"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser(description="Ingest git commits into the event log.")
    ap.add_argument("--since", type=int, default=7, help="days of history (default 7)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else CONFIG_DEFAULT
    if not cfg_path.exists():
        print(json.dumps({"status": "error", "error": f"config not found: {cfg_path}"}))
        sys.exit(1)

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    ok = errors = repos_scanned = 0

    for repo in cfg.get("repos", []):
        path = Path(repo["path"])
        name = repo.get("name", path.name)
        if not path.exists():
            if args.verbose:
                print(f"[skip] {path} (not found)", file=sys.stderr)
            continue
        repos_scanned += 1

        for c in git_log(path, args.since):
            actor = f"{name}:{c['short_sha']}"
            payload = {
                "summary": f"{name} [{c['short_sha']}] {c['subject']}",
                "repo": name,
                "repo_path": str(path),
                "sha": c["sha"],
                "short_sha": c["short_sha"],
                "author_name": c["author_name"],
                "author_email": c["author_email"],
                "subject": c["subject"],
                "body": c["body"],
            }
            if record(c["ts"], actor, payload):
                ok += 1
            else:
                errors += 1

    print(json.dumps({
        "status": "ok", "repos_scanned": repos_scanned,
        "commits_recorded_or_deduped": ok, "errors": errors,
    }))


if __name__ == "__main__":
    main()
