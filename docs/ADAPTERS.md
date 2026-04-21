# Writing an Adapter

An adapter is a script that pulls data from one source and writes events into `events.db`. Every source gets its own adapter. This is the longest-lived pattern in Open Brane — worth 10 minutes of reading before you write your first one.

## The shape

```python
#!/usr/bin/env python3
"""
ingest_<source>.py — pull <source> into events.db.
"""
def main():
    known_actors = load_existing_actors_for_source("<source>")

    for item in fetch_from_source():
        actor = fingerprint(item)
        if actor in known_actors:
            continue

        payload = {
            "summary": short_human_text(item),
            # ...everything else you want queryable
        }

        record_event(
            source="<source>",
            type=classify(item),
            actor=actor,
            ts=item.timestamp_iso,
            payload=payload,
        )
```

That's it. Every adapter is this.

## Hard rules

1. **One source per adapter.** If you're tempted to pull ClickUp tasks *and* ClickUp comments in one file, stop. Make two adapters (`ingest_clickup_tasks.py`, `ingest_clickup_comments.py`).

2. **Dedup via `actor`.** The `actor` column is where you stash a stable fingerprint for each source item — commit SHA, ClickUp task ID, Stripe invoice ID, a hash of URL+timestamp, whatever. Check `events.db` for existing `(source, actor)` pairs before writing. Idempotent re-runs are non-negotiable.

3. **Write through `record_event.py`, not direct SQL.** Even though you could open `events.db` and `INSERT INTO events`, don't. Go through the one write path:
   ```python
   subprocess.run(["python3", "record_event.py", "--source", ..., "--payload-stdin"],
                  input=json.dumps(payload))
   ```
   This keeps the write path singular forever. If you add validation, auditing, or event hooks to `record_event.py` later, every adapter benefits.

4. **Emit a JSON summary on stdout.** Last line of your adapter:
   ```python
   print(json.dumps({"ok": N, "skipped": N, "error": N}))
   ```
   Cron can grep for errors. Humans can eyeball runs.

5. **Exit 0 unless infra is broken.** Zero new items is a successful run. Network error is `exit(1)`.

6. **Always include `summary` in payload.** The embedder uses it. If you forget, embedding falls back to a synthesized descriptor, but it's never as good as what you'd write yourself.

## Picking a dedup fingerprint

The fingerprint goes in `actor`. It must be **stable** and **unique within the source**.

| Source | Good fingerprint | Bad fingerprint |
|---|---|---|
| Git commit | `<repo-name>:<short-sha>` | commit subject (mutable) |
| ClickUp task | `clickup-task-<id>` | task name (user-editable) |
| Stripe invoice | `stripe-inv-<id>` | customer email (changeable) |
| Google Doc | `gd-<sha256(path)[:16]>` | filename (renameable) |
| Fathom recording | `fathom-<meeting-id>` | meeting title |
| Slack message | `slack-<channel>-<ts>` | message text |
| Webhook event | `<source>-<event-id>` | received timestamp (retries change it) |

If the source gives you a stable ID, use it. If not, hash the most stable identifier (URL, path, author+timestamp). Prefix with the source short code so you can grep `actor LIKE 'clickup-%'` later.

## Handling updates to existing items

Open Brane is append-only. If a ClickUp task's status changes, you write a **new event** with a new actor (e.g. `clickup-task-<id>-v2` or include the `updated_at` in the hash). Downstream consumers read the latest event per task ID.

Two patterns work:

**Version in actor:**
```python
# Every status change = new event with new actor
actor = f"clickup-task-{task.id}-{hash(task.updated_at)[:8]}"
```

**Stable actor, `type` encodes change:**
```python
# First time: type="task.created"
# Subsequent: type="task.updated"
actor = f"clickup-task-{task.id}"
type = "task.updated" if task.id in known_actors else "task.created"
```

Pick one. The first makes history more explicit (every version is a separate row). The second makes "current state" queries simpler (just `MAX(ts) WHERE actor=?`).

## A minimal adapter example: ClickUp tasks

```python
#!/usr/bin/env python3
"""
ingest_clickup_tasks.py — pull ClickUp tasks into events.db.

Reads CLICKUP_API_KEY from env. Requires a list of team/space IDs in
config/clickup-sources.json.
"""
import json, os, sqlite3, subprocess, sys
from pathlib import Path
import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
DB = Path(os.environ.get("BRAIN_DB", SCRIPT_DIR.parent / "events.db"))
RECORD = SCRIPT_DIR / "record_event.py"
API_KEY = os.environ["CLICKUP_API_KEY"]

SOURCE = "clickup"


def known_actors() -> set[str]:
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(
            "SELECT actor FROM events WHERE source = ?", (SOURCE,)
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if r[0]}


def fetch_tasks(team_id: str) -> list[dict]:
    r = httpx.get(
        f"https://api.clickup.com/api/v2/team/{team_id}/task",
        headers={"Authorization": API_KEY},
        params={"page": 0, "include_closed": "true"},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json().get("tasks", [])


def record(ts: str, actor: str, payload: dict) -> bool:
    proc = subprocess.run(
        ["python3", str(RECORD),
         "--source", SOURCE, "--type", "task",
         "--actor", actor, "--ts", ts, "--payload-stdin"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    return proc.returncode == 0


def main():
    cfg = json.loads((SCRIPT_DIR.parent / "config" / "clickup-sources.json").read_text())
    already = known_actors()
    ok = skipped = errors = 0

    for team_id in cfg["team_ids"]:
        for task in fetch_tasks(team_id):
            actor = f"clickup-task-{task['id']}-{hash(task['date_updated'])[:8]}"
            if actor in already:
                skipped += 1
                continue

            payload = {
                "summary": f"[{task['status']['status']}] {task['name']}",
                "task_id": task["id"],
                "name": task["name"],
                "status": task["status"]["status"],
                "assignees": [a["username"] for a in task.get("assignees", [])],
                "priority": (task.get("priority") or {}).get("priority"),
                "due_date": task.get("due_date"),
                "list": (task.get("list") or {}).get("name"),
                "space": (task.get("space") or {}).get("id"),
                "url": task.get("url"),
            }
            ts = task["date_updated"]  # ClickUp gives ms epoch as string
            if record(ts, actor, payload):
                ok += 1
            else:
                errors += 1

    print(json.dumps({"status": "ok", "ok": ok, "skipped": skipped, "errors": errors}))


if __name__ == "__main__":
    main()
```

~70 lines. Every adapter you'll ever write is a variation on this.

## Webhook-driven adapters (Stripe, Fathom, etc.)

For sources that push instead of pull, run a tiny FastAPI server and have the webhook handler call `record_event.py`:

```python
# webhook_ingest.py
from fastapi import FastAPI, Request, HTTPException
import json, subprocess, os
from pathlib import Path

app = FastAPI()
RECORD = Path(__file__).parent / "record_event.py"

@app.post("/stripe")
async def stripe_webhook(req: Request):
    sig = req.headers.get("Stripe-Signature")
    # (validate signature with STRIPE_WEBHOOK_SECRET - omitted)
    event = await req.json()

    payload = {
        "summary": f"Stripe {event['type']} {event['data']['object'].get('id', '')}",
        "stripe_event": event,
    }

    proc = subprocess.run(
        ["python3", str(RECORD),
         "--source", "stripe",
         "--type", event["type"],
         "--actor", f"stripe-event-{event['id']}",
         "--ts", event.get("created_iso", ""),
         "--payload-stdin"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise HTTPException(500, proc.stderr.strip())
    return {"status": "ok"}
```

Point Stripe/Fathom/Slack at your webhook. Each event becomes one row in `events.db`. Idempotent via the provider's `event_id`.

## Testing an adapter

1. **Dry-run mode.** Add `--dry-run` that does everything except call `record_event.py`. Shows you what *would* be written.
2. **One-shot test.** Run the adapter against a small subset (e.g. `--limit 5`). Verify rows landed: `./query_events.py --source <source> --limit 5`.
3. **Re-run test.** Run twice. Second run should write zero events.
4. **Deletion-resilience test.** Delete one event row (`DELETE FROM events WHERE id=...`). Re-run. That event should re-appear (proves the dedup is doing its job *only* via the `known_actors` set, not some external cache).

## Cron pattern

```cron
# Every 5 minutes for cheap fetches
*/5 * * * * cd /var/lib/open-brane && ./scripts/ingest_git.py >> logs/cron.log 2>&1

# Every 15 for API calls that rate-limit
*/15 * * * * cd /var/lib/open-brane && ./scripts/ingest_clickup_tasks.py >> logs/cron.log 2>&1

# Hourly for things that don't change often
5 * * * * cd /var/lib/open-brane && ./scripts/ingest_web_sources.py >> logs/cron.log 2>&1

# Daily for one-off enrichment
0 3 * * * cd /var/lib/open-brane && ./scripts/compile_views.py >> logs/cron.log 2>&1
```

Watch `logs/cron.log`. Grep for `"errors": [1-9]` to find failures.

## Anti-patterns

- **Writing directly to `events.db`.** Never. Always through `record_event.py`.
- **Embedding inside the adapter.** Embedding is a separate cron (`embed_events.py`). Don't couple.
- **Recording derived metrics.** Record raw events. Compute MRR / churn / NPS in a SQL query against the raw events, on demand.
- **Silent failures.** If the fetch API errors, `sys.exit(1)` and log to stderr. Don't swallow.
- **Stateful adapters.** No local caches, no scratch files. Dedup lives in `events.db`. If you restart the VPS, nothing breaks.
- **Orchestrator scripts that call adapters.** Cron calls adapters directly. No wrappers. No queue managers. Cron *is* the orchestrator.

## Adapters worth writing

Here's a list, roughly ordered by value-per-hour:

| Source | Why | Time to write |
|---|---|---|
| Google Drive | Most teams' real doc store. Already shipped (`ingest_gdrive.py`). | 0 |
| Git | Changelog, authorship, what happened when. Already shipped (`ingest_git.py`). | 0 |
| Claude Code sessions | Your own thinking + agent output, searchable. Already shipped. | 0 |
| ClickUp / Linear / Jira | Makes task status queryable from the same place as everything else. | 1-2 hrs |
| Stripe | MRR / churn / refunds as raw events. Definition layer on top. | 1 hr (webhook) |
| Fathom / Otter / Granola | Meeting transcripts as events. Semantic search across all calls. | 1 hr |
| Gmail / Google Calendar | The oldest, richest personal data most people have. | 2-3 hrs (OAuth) |
| Slack | All DMs and channels. High noise but high value for "when did we decide X". | 2 hrs |
| Webhooks in general | Catch-all for things with event feeds. FastAPI + `record_event`. | 30 min per source |

Start with the source that's causing the most pain. Each adapter ships in a sitting.
