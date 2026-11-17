#!/usr/bin/env python3
"""Initialize or run the existing Open Brane tools from one portable config."""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from runtime_config import ROOT, apply_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "init", "run", "doctor", "embed", "serve", "search", "reindex"))
    parser.add_argument("query", nargs="?", help="words to find (search only)")
    parser.add_argument("--limit", type=int, default=10, help="results to print (search only)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7778)
    args = parser.parse_args()
    try:
        config = apply_config(args.config)
        db = Path(os.environ["BRAIN_DB"])
        if args.command == "validate":
            print(json.dumps({"status": "ok", "business_id": config["business_id"], "database": str(db)}))
            return 0
        if args.command == "init":
            db.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(db)) as conn:
                for name in ("SCHEMA.sql", "schemas/governance.sql", "schemas/recall_traces.sql", "schemas/edges.sql"):
                    conn.executescript((ROOT / name).read_text(encoding="utf-8"))
            auth_db = Path(os.environ["BRAIN_AUTH_DB"])
            auth_db.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(auth_db)) as auth_conn:
                auth_conn.executescript((ROOT / "schemas/customer.sql").read_text(encoding="utf-8"))
            Path(os.environ["BRAIN_ARTIFACT_ROOT"]).mkdir(parents=True, exist_ok=True)
            print(json.dumps({"status": "ok", "database": str(db), "business_id": config["business_id"], "vectors_enabled": config.get("vectors", {}).get("enabled", False)}))
            return 0
        if not db.exists():
            raise ValueError("database missing; run starter.py init first")
        if args.command == "serve":
            os.execv(sys.executable, [sys.executable, str(ROOT / "scripts/http_server.py"),
                                     "--config", str(Path(args.config).resolve()),
                                     "--host", args.host, "--port", str(args.port)])
        if args.command == "run":
            return subprocess.call([sys.executable, str(ROOT / "scripts/mcp_server.py")])
        if args.command == "reindex":
            # The text index is derived from events; dropping and rebuilding it loses nothing.
            import search_index
            with closing(sqlite3.connect(db, timeout=30)) as conn:
                print(json.dumps({"status": "ok", "indexed": search_index.rebuild(conn)}))
            return 0
        if args.command == "search":
            import search_index
            if not args.query or not args.query.strip() or not 1 <= args.limit <= 1000:
                raise ValueError('search needs words to find and a limit of 1..1000, e.g. search "pricing decision"')
            with closing(sqlite3.connect(db, timeout=30)) as conn:
                rows = search_index.search(conn, args.query, limit=args.limit)
            for row in rows:
                print(f"#{row['id']}  {row['ts']}  {row['source']}/{row['type']}  [{row['match']}]\n    {row['snippet']}")
            if not rows:
                print("No matching events.", file=sys.stderr)
            return 0
        if args.command == "embed":
            if not config.get("vectors", {}).get("enabled", False):
                # Core installs do not have the vector client; say so instead of a traceback.
                raise ValueError("vector indexing disabled in deployment config; set vectors.enabled and install requirements.txt")
            return subprocess.call([sys.executable, str(ROOT / "scripts/embed_events.py")])
        # A core-only installation does not require embedding services.
        with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            import search_index
            indexed = search_index.status(conn)["indexed"]
        vectors = config.get("vectors", {}).get("enabled", False)
        report = {"status": "ok" if integrity == "ok" else "error", "database_healthy": integrity == "ok", "events": count, "text_search_indexed": indexed, "vectors_enabled": vectors, "provider": config.get("crm", {}).get("provider", "event")}
        if vectors:
            proc = subprocess.run([sys.executable, str(ROOT / "scripts/health_check.py")], capture_output=True, text=True)
            health = json.loads(proc.stdout) if proc.returncode == 0 else {"all_healthy": False, "error": proc.stderr.strip()}
            report["vector_health"] = health
            if not health.get("all_healthy"):
                report["status"] = "degraded"
        else:
            report["vector_health"] = {"status": "disabled", "semantic_search_available": False}
        print(json.dumps(report))
        return 0 if report["status"] == "ok" else 1
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
