# Pattern Scanner

A daily reflection loop for an event-log brain. Reads the last week of session events, surfaces cross-project tensions and open questions, lets you rate them, researches the survivors, and promotes the ones that keep coming back true.

The whole thing runs on the same `events.db` you already have. No new datastore. No separate dashboard. Every artifact is a render over events.

## Why bother

You write a lot. Across many projects. The same uncertainty shows up in three different sessions and you forget you've seen it before. The same guess gets confidently asserted, then quietly contradicted next month, and you never close the loop.

A pattern scanner is the layer that says: *here are the things you keep almost-deciding, ranked by how often they came up, with the older context attached.* You read it for two minutes a day. You click `discarded` on the noise, `act` on the things you're going to do, and `kept` on the things to watch. The system gets a tiny bit smarter every time.

## The loop

```
   1. SCAN              2. RATE                3. RESEARCH           4. PROMOTE
   scanner          →   rating UI         →   researcher       →    scanner
   (cron)               (HTTP)                (cron)                (next day)
   ─────────────        ─────────────         ─────────────         ─────────────
   read N days          you click             adversarial LLM       kept ≥ 3 →
   per-project          act/kept/discarded    + brain search        promotion-
   digest               /wrong/stale/         + external search     suggestion
   cross-project        surprising/note       (gh, web, etc.)
   synthesis            → feedback events     → research events     discarded/wrong
   write events                                                     → suppressed
```

The four stages share one substrate: events with `source='pattern-scan'`. There is no separate "patterns" table.

## Maturity tiers

A finding moves through three tiers as evidence accumulates:

```
NOTICED       →  GUESSED       →  PLAYBOOK
(question)        (guess)          (confirmed)
```

Stored as event `type`:

| Type | Meaning |
|---|---|
| `question` | Noticed but unanswered. Surface the *shape* of the unresolved thing. |
| `guess` | Tentative answer, low confidence. The LLM narrates a current best take from the cluster. |
| `observation` | A 1-line note that doesn't rise to a question (e.g. "you've reinvented X four times"). |
| `playbook` | Settled. Promoted by hand after multiple sessions independently confirm it. |
| `contradiction` | A later session disagrees with a playbook entry. Flagged for review. |
| `research` | Output of the adversarial + web pass on a specific question/guess. |
| `feedback` | Your verdict on a finding. |
| `promotion-suggestion` | Emitted when a finding accumulates enough positive feedback to be a playbook candidate. |

## What the scanner emits

Every finding is one event. Minimal payload shape:

```json
{
  "source": "pattern-scan",
  "type": "question",
  "actor": "ab12cd34ef56",
  "ts": "2026-05-01T04:45:00Z",
  "payload_json": {
    "text": "Why does the redirect rule fire on /admin but not /admin/?",
    "projects": ["case-engine", "kaicalls"],
    "evidence": "...",
    "lineage": ["older event ids that touched the same shape"],
    "urgency": "medium"
  }
}
```

The `actor` is a stable slug (sha1 of canonical question text, first 12 chars). Re-running the scanner on overlapping windows is idempotent because the same text yields the same actor.

## Feedback verdicts

The rating UI writes one event per click:

```json
{
  "source": "pattern-scan",
  "type": "feedback",
  "actor": "feedback-<short-uuid>",
  "payload_json": {
    "target_actor": "ab12cd34ef56",
    "target_type": "question",
    "verdict": "kept",
    "note": "optional one-liner"
  }
}
```

Seven verdicts, with deliberate asymmetry:

| Verdict | Effect on next scan |
|---|---|
| `discarded` / `wrong` | Finding is suppressed by slug AND its text is injected into the synthesis prompt as "do not raise this again" — semantic dedup, not just exact-string. |
| `kept` ≥ 3 | Scanner emits a `promotion-suggestion` event referencing the finding. |
| `act` | Same as `kept ≥ 3`, immediately. Stronger because you actually did something. |
| `stale` | No suppression, no promotion. Records that it *was* useful but no longer is. |
| `surprising` | No suppression, no promotion. Records that the scanner caught something you wouldn't have. |
| `note` | Neutral. Stores a thought alongside the finding. |

Aggregation: pull every `feedback` event, group by `target_actor`, count verdicts. `discarded > 0 OR wrong > 0` ⇒ suppress. `kept ≥ 3 OR act > 0` ⇒ promotion candidate.

## What you need to build

Four pieces. None of them are large.

### 1. The scanner

Reads recent events grouped by project. For each project, asks an LLM "what's unresolved here?" → digest. Then asks the LLM to find tensions across all the digests → questions, guesses, observations. Writes each as an event.

Knobs that matter:
- **Window**: 7 days is the right default. 30/90 are useful for retros.
- **Per-project cap**: you'll OOM otherwise. ~120 events × ~400 chars works on a 24 GB GPU with a 7B-class local model.
- **Wide-window mode**: for windows > 14 days, skip the per-project digest and feed compressed event summaries straight into synthesis. Cuts a 90-day scan from 30+ minutes to a few minutes.
- **Lineage**: for each finding, search the brain for older events that touched the same shape. Exclude events inside the scan window so the current cluster doesn't appear as its own ancestor.

### 2. The rating UI

A tiny stdlib HTTP server. Two endpoints:

- `GET /` — render every open finding with rate buttons, current vote tallies, and any notes.
- `POST /rate` — `{actor, verdict, note?}` → write one `feedback` event.

That's it. No auth (run it on a private network). No framework. Mine is ~700 lines of stdlib Python and that's mostly the inline HTML.

The UI is the entire product surface. If the buttons aren't there, you won't rate. If you don't rate, the scanner can't learn.

### 3. The researcher (optional but recommended)

Picks up open questions and guesses, runs each through:
- An adversarial pass on the same local LLM ("what's wrong with this guess? what would make it sharper?")
- A semantic search over older events to surface relevant prior thinking
- A budgeted external search (GitHub issues, web research, deep research API) — questions-first, urgency-weighted

Writes one `research` event per finding it touched. Daily budget caps prevent runaway spend on the external calls.

### 4. The schedule

```
04:45  scanner     # write findings
05:00  researcher  # adversarial + lineage + external
```

Plus a long-running rating UI as a systemd unit (or Docker container, or whatever you use).

## Example setup

A minimum viable wiring on top of an existing `events.db`. ~150 lines of code total once you write the scanner and the UI; the rest is config.

**1. Pick a model.** Anything that returns JSON reliably on a 4–6k-char prompt. A local 7B-class model on a 24 GB GPU is enough; a hosted API works too. The scanner runs once a day so latency doesn't matter much.

**2. Write `scripts/scan.py`.** Skeleton:

```python
def main(since_days=7):
    events = query_events(since=since_days, sources=["claude", "git", "your-others"])
    feedback = load_feedback()  # suppressed actors + promotion candidates
    by_project = group_by_project(events)
    digests = {p: llm_digest(evts) for p, evts in by_project.items()}
    findings = llm_synthesize(digests, suppress=feedback["discard_texts"])
    for f in findings:
        actor = sha1(f["text"])[:12]
        if actor in feedback["suppressed_actors"]:
            continue
        record_event(source="pattern-scan", type=f["type"], actor=actor, payload=f)
    for cand in feedback["promotion_candidates"]:
        record_event(source="pattern-scan", type="promotion-suggestion",
                     actor=f"promo-{cand['target_actor']}", payload=cand)
    write_markdown_report(findings)
```

The two LLM calls are the only bits with prompt-engineering. Per-project digest: "what's unresolved in this project's last 7 days?" Synthesis: "across these digests, what tensions or open questions or guesses come up?"

**3. Write `scripts/rate_server.py`.** Stdlib HTTP. Two routes:

```python
# GET /  →  render every open finding + tallies + buttons
# POST /rate  →  {actor, verdict, note?}  →  record_event(type="feedback", payload={target_actor, verdict, note})
```

Run it on `localhost:8766` (or any port). One short HTML template. Seven buttons per finding.

**4. (Optional) Write `scripts/research.py`.** Picks open questions + guesses, asks the same LLM "what's wrong with this guess?", does a semantic search for lineage, optionally hits external search APIs with daily caps. Writes a `research` event per finding it touched.

**5. Wire it up.**

```bash
# crontab
45 4 * * *  cd ~/brain && .venv/bin/python scripts/scan.py --since-days 7
0  5 * * *  cd ~/brain && .venv/bin/python scripts/research.py    # if you wrote it

# systemd unit (or just nohup it)
ExecStart=/home/you/brain/.venv/bin/python /home/you/brain/scripts/rate_server.py
```

**6. Use it.** Tomorrow morning, open `http://localhost:8766/`. Click `discarded` on noise. Click `kept` on things to watch. Click `act` on things you're going to do today. Two minutes a day. Day 30 you have a `topics/open-questions.md` page that's been pruned by your own taste and a handful of `promotion-suggestion` events waiting to become playbook entries.

## Why this design

**Events all the way down.** No separate "patterns" table, no cache to invalidate. The current state of any finding is a function of all the events about it. If you delete the rating UI tomorrow, the next scan still works — it just stops getting feedback.

**Suggestion-only promotion.** The scanner never auto-writes a playbook entry. It only emits `promotion-suggestion`. You hand-write the playbook so the framing is yours. The system finds the candidates; you decide what they mean.

**Suppression is text-aware.** Suppressing by `actor` slug alone leaks — the scanner will phrase the same question slightly differently next week and it'll come right back. Injecting the suppressed texts into the synthesis prompt with "do not raise this again" makes the LLM dedup semantically. Cheap and effective.

**One-user assumption.** Multiple raters would race on the same `actor`. If you need that, switch verdict aggregation to a join over `(target_actor, rater)` and you're fine.

## Reference implementation

The author's private brain has this running. Files (not yet ported as standalone scripts to this repo):

- `bin/scan.py` — stages 1 + 4
- `bin/rate_server.py` — stage 2 (HTTP UI)
- `bin/rate.py` — stage 2 (CLI alternative)
- `bin/research.py` — stage 3
- `bin/run_research.sh` — env wrapper for cron

Roughly 2,000 lines total. Stdlib + `sqlite3` + an LLM client. If you fork the brain, the simplest path is to drop these in alongside `scripts/record_event.py` — every read-write goes through the same single-write-path discipline.

## See also

- [`SCHEMA.sql`](../SCHEMA.sql) — the events table this all reads and writes
- [`scripts/record_event.py`](../scripts/record_event.py) — the single write path; the rating UI calls this
- [`scripts/semantic_search.py`](../scripts/semantic_search.py) — used for lineage + suppressed-text dedup
- [`docs/CONDUCTOR-FLOW.md`](./CONDUCTOR-FLOW.md) — the upstream pattern: agents decide which deterministic worker to run
