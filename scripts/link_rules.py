#!/usr/bin/env python3
"""
link_rules.py — rule-based auto-linker for typed reasoning edges.

Reads events.db, applies a small set of deterministic rules, writes edges to
event_edges. Idempotent: UNIQUE (from_event_id, to_event_id, relation) on
event_edges means re-running this script does nothing the second time
unless new events have landed.

Run from cron alongside embed_events.py:

    0 */4 * * * cd /var/lib/open-brane && .venv/bin/python scripts/link_rules.py >> logs/cron.log 2>&1

Requires schemas/edges.sql to be applied. Skips silently if not.

Each rule is a function that returns a list of edge dicts. Add new rules by
appending a function and registering it in RULES below.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

DB = Path(os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))


# ---------------------------------------------------------------------------
# Rule 1 — auto-capture session_summary <-> atomic ACT NOW events
# ---------------------------------------------------------------------------
def rule_session_summary_relations(conn: sqlite3.Connection) -> list[dict]:
    """Link every session_summary event to each event in its atomic_event_ids list.

    Bidirectional `related_to` edges — the summary is related to its items,
    and (separately) each item is related to its summary. The view
    `event_support` and ad-hoc queries can traverse either direction.
    """
    cur = conn.execute(
        "SELECT id, payload_json FROM events "
        "WHERE source = 'session' AND type = 'session_summary'"
    )
    edges: list[dict] = []
    for summary_id, payload_json in cur:
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            continue
        atomic_ids = payload.get("atomic_event_ids") or []
        if not isinstance(atomic_ids, list):
            continue
        for atomic_id in atomic_ids:
            if not isinstance(atomic_id, int) or atomic_id == summary_id:
                continue
            # Summary -> atomic
            edges.append({
                "from_event_id": summary_id,
                "to_event_id":   atomic_id,
                "relation":      "related_to",
                "weight":        0.9,
                "rationale":     "auto: session_summary lists this in atomic_event_ids",
                "created_by":    "rule:session_summary_relations",
            })
            # Atomic -> summary (so each ACT NOW can find its parent)
            edges.append({
                "from_event_id": atomic_id,
                "to_event_id":   summary_id,
                "relation":      "informs",
                "weight":        0.7,
                "rationale":     "auto: atomic ACT NOW belongs to this session_summary",
                "created_by":    "rule:session_summary_relations",
            })
    return edges


# ---------------------------------------------------------------------------
# Rule 2 — panning-for-gold ACT NOW <-> open_question / parked from same session
# ---------------------------------------------------------------------------
def rule_panning_session_relations(conn: sqlite3.Connection) -> list[dict]:
    """All panning-for-gold events with the same session_id are related_to each other.

    Skill writes one session per pan; threads in the same pan share context.
    """
    # Group by session_id
    sessions: dict[str, list[int]] = {}
    cur = conn.execute(
        "SELECT id, payload_json FROM events WHERE source = 'panning'"
    )
    for event_id, payload_json in cur:
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            continue
        sid = payload.get("session_id")
        if not sid:
            continue
        sessions.setdefault(sid, []).append(event_id)

    edges: list[dict] = []
    for sid, ids in sessions.items():
        if len(ids) < 2:
            continue
        # Connect each pair (one direction; sparse — only consecutive)
        for i in range(len(ids) - 1):
            edges.append({
                "from_event_id": ids[i],
                "to_event_id":   ids[i + 1],
                "relation":      "related_to",
                "weight":        0.6,
                "rationale":     f"auto: same panning session_id={sid}",
                "created_by":    "rule:panning_session_relations",
            })
    return edges


# ---------------------------------------------------------------------------
# Rule 3 — same-actor supersession for ClickUp/git events
# ---------------------------------------------------------------------------
def rule_same_actor_supersession(conn: sqlite3.Connection) -> list[dict]:
    """When two events share (source, actor) and differ in time, the newer
    `supersedes` the older.

    Useful for clickup (same task ID re-ingested with new status), git (same
    file path re-touched), stripe (same subscription re-billed). Only emits
    one edge per actor — newest -> 2nd newest. Older history is reachable by
    walking the chain.
    """
    cur = conn.execute(
        "SELECT source, actor, MAX(id) AS newest_id, "
        "       (SELECT id FROM events e2 "
        "        WHERE e2.source = events.source AND e2.actor = events.actor "
        "        ORDER BY id DESC LIMIT 1 OFFSET 1) AS prior_id "
        "FROM events "
        "WHERE actor IS NOT NULL AND source IN ('clickup','git','stripe') "
        "GROUP BY source, actor "
        "HAVING COUNT(*) >= 2"
    )
    edges: list[dict] = []
    for src, actor, newest_id, prior_id in cur:
        if not prior_id or newest_id == prior_id:
            continue
        edges.append({
            "from_event_id": newest_id,
            "to_event_id":   prior_id,
            "relation":      "supersedes",
            "weight":        0.85,
            "rationale":     f"auto: same {src} actor={actor[:40]}",
            "created_by":    "rule:same_actor_supersession",
        })
    return edges


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------
RULES = [
    rule_session_summary_relations,
    rule_panning_session_relations,
    rule_same_actor_supersession,
]


def apply_edges(conn: sqlite3.Connection, edges: list[dict]) -> tuple[int, int]:
    """Insert edges, skipping ones that already exist. Returns (created, skipped)."""
    created = 0
    skipped = 0
    for e in edges:
        try:
            conn.execute(
                "INSERT INTO event_edges (from_event_id, to_event_id, relation, "
                "weight, rationale, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (e["from_event_id"], e["to_event_id"], e["relation"],
                 e["weight"], e["rationale"], e["created_by"]),
            )
            created += 1
        except sqlite3.IntegrityError:
            # UNIQUE constraint OR self-loop CHECK
            skipped += 1
    return created, skipped


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON;")

    # Schema check
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='event_edges'"
    ).fetchone()
    if not row:
        print(json.dumps({"status": "skipped", "reason": "edges schema not applied"}))
        conn.close()
        return 0

    report: dict = {"rules": [], "totals": {"created": 0, "skipped": 0, "errors": 0}}

    for rule in RULES:
        try:
            edges = rule(conn)
            created, skipped = apply_edges(conn, edges)
            conn.commit()
            report["rules"].append({
                "name": rule.__name__,
                "candidates": len(edges),
                "created": created,
                "skipped": skipped,
            })
            report["totals"]["created"] += created
            report["totals"]["skipped"] += skipped
        except Exception as exc:
            report["rules"].append({
                "name": rule.__name__,
                "error": str(exc),
            })
            report["totals"]["errors"] += 1

    conn.close()
    print(json.dumps(report, indent=2))
    return 0 if report["totals"]["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
