# Conductor & Flow — Long-Running Multi-Agent Orchestration

When a task takes 50 minutes and touches five agents, context windows blow up and coordination falls apart. Claude Code's native sub-agent spawning is great for coding (errors happen, sub-agents report back, parent re-plans), but it's wrong for non-coding multi-step workflows where there's no obvious failure signal and you just need state to persist across hops.

Two patterns are worth knowing, both of which complement Open Brane's event log.

## Google's Flow pattern (from the Agent Development Kit)

**The idea:** Long-running agent workflows get their own markdown orchestration file that persists state between agent hops. Each agent reads the file at the start of its turn, does its work, updates the file, and stops.

**The file is the memory.** Not the prompt. Not the agent's internal scratchpad. A literal markdown (or YAML or JSON) file with a known structure.

**Example: a "write this case study" workflow across five agents**

```markdown
---
workflow: case-study
subject: HealthCo/Dr-Jane-Smith
status: in_progress
current_step: 3
started_at: 2026-04-21T10:00:00Z
---

## Step 1: Research — COMPLETE
Owner: research-agent
Completed: 2026-04-21T10:04:12Z
Output: https://drive.google.com/file/d/abc123 (research brief)
Event ID: 4711

## Step 2: Interview Transcript Analysis — COMPLETE
Owner: analysis-agent
Completed: 2026-04-21T10:09:33Z
Key findings:
- Customer achieved 14hr/week savings on prior auth
- Main objection: board skepticism about AI accuracy
- Most quoted moment: 4:22 ("real unlock was the rule, not the AI")
Event ID: 4718

## Step 3: Draft — IN PROGRESS
Owner: writer-agent
Started: 2026-04-21T10:10:00Z
Outline:
- Hook: 14hrs lost to prior auth
- Problem: manual approvals + staffing crunch
- Solution: rule-based triage, AI as second opinion
- Results: [pull from step 2]
- CTA: book a demo
Draft location: drafts/case-study-healthco-v1.md

## Step 4: Review — PENDING
Owner: editor-agent

## Step 5: Publish — PENDING
Owner: publish-agent
```

Every agent reads this file first. Knows exactly where the workflow is. Knows what's been done, what's pending, where to find prior artifacts. Updates its section, marks status, moves on.

No agent ever has to "remember" what the previous agent did. The file is the memory.

## Why this beats hierarchical sub-agent spawning for non-coding tasks

Claude Code's `Task` tool spawns a sub-agent, the sub-agent runs in its own context window, and returns a single message to the parent. This works beautifully when:
- The sub-agent's work is self-contained
- There's an obvious pass/fail signal (tests, compile errors)
- The parent can re-plan if the sub-agent fails

It works poorly when:
- Step 3 needs the full artifact from step 2, not a summary
- There's no pass/fail signal (what does "failure" mean for a draft paragraph?)
- Eval is subjective and you want a human checkpoint between steps

Flow-style orchestration works better here because:
- State is inspectable at any moment (just read the file)
- You can drop a human into the middle ("hey, step 3 is stuck, here's the context")
- Re-running from step 3 is trivial (state is already there)
- You're not re-generating summaries for every hop

## How to use Flow with Open Brane

Flow files are **ephemeral state**. Events are **durable truth**. Use both.

### Start a workflow

```python
# workflow_start.py — called by whatever triggers a new workflow
import json, uuid, subprocess
from pathlib import Path
from datetime import datetime, timezone

workflow_id = str(uuid.uuid4())[:8]
flow_path = Path(f"workflows/case-study-{workflow_id}.md")
flow_path.parent.mkdir(exist_ok=True)

flow_content = f"""---
workflow: case-study
workflow_id: {workflow_id}
subject: {subject}
status: in_progress
current_step: 1
started_at: {datetime.now(timezone.utc).isoformat()}
---

## Step 1: Research — PENDING
Owner: research-agent

## Step 2: Analysis — PENDING
Owner: analysis-agent

## Step 3: Draft — PENDING
Owner: writer-agent

## Step 4: Review — PENDING
Owner: editor-agent

## Step 5: Publish — PENDING
Owner: publish-agent
"""
flow_path.write_text(flow_content)

# Record the workflow start as an event
subprocess.run(
    ["python3", "scripts/record_event.py",
     "--source", "workflow", "--type", "workflow.started",
     "--actor", f"wf-{workflow_id}",
     "--payload-stdin"],
    input=json.dumps({
        "summary": f"case-study workflow started for {subject}",
        "workflow_id": workflow_id,
        "flow_file": str(flow_path),
    }),
)
```

### Run a step

Each step is an agent invocation with three inputs: the flow file, the prior events for this workflow, and the current step's instructions.

```python
# run_step.py
def run_step(workflow_id: str, step_num: int):
    flow_path = Path(f"workflows/case-study-{workflow_id}.md")
    flow_content = flow_path.read_text()

    # Find prior events for context
    prior_events = subprocess.run(
        ["python3", "scripts/query_events.py",
         "--actor", f"wf-{workflow_id}", "--limit", "50", "--order", "asc"],
        capture_output=True, text=True,
    ).stdout

    # Build agent prompt
    prompt = f"""You are step {step_num} in a multi-agent workflow.

Flow file (current state):
{flow_content}

Prior events from this workflow:
{prior_events}

Your job: execute step {step_num}. Update the flow file when complete.
Record your output as an event via record_event(source="workflow",
type="step.{step_num}.complete", actor="wf-{workflow_id}",
payload={{summary: ..., output_ref: ...}}).
"""

    # Invoke the agent (via MCP, CLI, HTTP — whatever your stack uses)
    invoke_agent(prompt)
```

### Where state goes

| Thing | Home |
|---|---|
| "What step are we on, what happened so far?" | Flow file (`workflows/case-study-<id>.md`) |
| "What was the actual research output?" | Event in `events.db`, artifact in Drive/Obsidian |
| "What was the final deliverable?" | Event with `attachment_uri` pointing at the rendered doc |
| "Replay this workflow on a new subject" | Flow template + new workflow_id |

Flow files are ephemeral — delete them after the workflow completes (or archive them). Events are permanent — they're the audit trail.

## When NOT to use Flow

- **One-shot tasks.** If the whole thing fits in one context window, just do it. Flow adds overhead.
- **Genuinely parallel work.** If steps 2 and 3 don't depend on each other, spawn them in parallel (Claude's `Task` tool, or explicit async). Flow is serial by design.
- **Coding tasks with test-based eval.** Sub-agent spawning + test feedback loop is already optimal. Flow is overkill.
- **Workflows you run once.** The upfront cost of setting up the flow template isn't worth it. Just do it by hand.

Use Flow when the same workflow repeats ≥5 times, has ≥3 steps, and each step produces an artifact that the next step consumes.

## The "Conductor" name

The Claude Code / Anthropic "Antigravity" and related work sometimes refers to this orchestration pattern as a Conductor — a named process that owns the workflow lifecycle. "Flow" is Google's ADK name for essentially the same idea. Different vendors, same pattern.

The name matters less than the shape: **a persistent file (or DB row) is the state, agents read/write it, no agent owns the whole lifecycle, rerunning from any step is trivial.**

## Further reading

- Google ADK (Agent Development Kit) — `google.github.io/adk-docs` (search "Flow")
- Anthropic's "Building effective agents" post — hierarchical vs flat orchestration tradeoffs
- `ghuntley.com/ralph` — the Ralph Wiggum autonomous loop pattern (related: memory via text files + git)

## Integration with Open Brane

Every workflow step should record an event. That way:
- You can query "how long did step 3 take on average across all case-study workflows" via SQL.
- You can replay a failed workflow from step N by reading the last successful event.
- Semantic search across workflow events finds prior similar runs ("what did we write last time for a fintech case study?").
- Workflow metrics come out of `events.db` for free — no separate observability stack.

The flow file gives you in-flight state. The event log gives you history and analytics. They're complementary.
