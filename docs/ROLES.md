# Hand work between agents by role

When several agents work for you, each usually has a job: one researches, one writes code, one drafts posts, one checks the others. The role task board gives that split a shared place to live. A lead (a person or an agent) posts a task for a role. Whichever agent works that role claims it, gets the context it needs to start, and settles it with proof. A different agent can verify the result before the next task in the chain starts from it.

Try it with scripted clients and temporary data (after `python3 brane.py setup`):

```bash
python3 brane.py demo roles
```

```
lead posted t_f579ffa94b5c for research and t_ac4bb973c735 for build
builder asked for work: nothing ready, 1 task waiting on research
researcher claimed t_f579ffa94b5c until 2026-11-27T13:29Z
settling as done without proof: refused
researcher settled done with proof event:4
builder asked again: research is done but not yet verified, so still waiting
researcher tried to verify its own work: refused
lead verified the research result
builder claimed t_ac4bb973c735 and received: "Five questions saved as a note." (proof event:4, verified by lead)
builder's briefing also lists 3 saved note(s) already matching its task title
board: build=claimed, research=done
```

The three clients are scripts, not autonomous agents. Nothing is sent and the data is deleted when the demo ends.

## The tools

| Tool | What it does |
|---|---|
| `post_task(role, title, brief, done_when, posted_by?, project_id?, depends_on?, context_event_ids?, request_id?, followup_to?, require_verified?, never?)` | Posts a task for one role. `done_when` is the line the result is judged against. `depends_on` lists task ids that must be done first; with `require_verified=true` they must also be verified. `project_id` links a saved project. `context_event_ids` points at notes the agent should read. `never` lists actions the agent must not take. `followup_to` replaces a blocked, failed or rejected task. A retried post with the same `request_id` returns the same task. |
| `claim_task(role, agent, lease_minutes=120, task_id?)` | Claims the oldest ready task for the role and returns it with a briefing. Returns `task: null` when nothing is ready, with a count of tasks waiting on dependencies. |
| `renew_claim(task_id, claim_id, agent, lease_minutes=120)` | Extends your own live claim, so a long job is not handed to a second agent. |
| `settle_task(task_id, claim_id, agent, status, outcome, proof?, lesson?)` | `status` is `done`, `blocked` or `failed`. `done` needs at least one checkable proof item (see below). For `blocked` and `failed`, say in `outcome` exactly what is missing. `lesson` is optional: `{pattern, change, metric?, before?, after?}`. |
| `verify_task(task_id, agent, verdict, note)` | A different agent checks a done task against its `done_when` and proof. `verdict` is `verified` or `rejected`. |
| `list_tasks(role?, status?, limit?)` | The board, newest first. Status is `open`, `waiting`, `stuck`, `claimed`, `done`, `blocked`, `failed` or `rejected`. |
| `role_brief(role)` | What a fresh agent in a role reads first: its open queue, active claims, waiting and stuck tasks, done work awaiting verification, and recent results with lessons. |

The browser home screen shows the same board (read-only), and `python3 brane.py tasks [--role build] [--status open]` prints it in the terminal. Over HTTP the board is `GET /api/tasks` with the Bearer token.

## What the briefing contains

`claim_task` returns saved facts only. It never guesses at progress.

- **Already known:** up to five saved notes and settled results whose text matches this task's title, found with the same ranked search as `search_events`. Read these before starting, so known work is not redone.
- **Recent outcomes for the role:** the last five settled tasks of that role, with outcome, proof, lesson and verification. An agent sees what the previous agent in its seat found or got stuck on.
- **Upstream results:** for each task this one depends on, its outcome, proof and verification. This is the handoff: the build agent starts from the research agent's result, not from a summary someone retyped.
- **Replaces:** for a follow-up, the outcome or rejection note of the task it replaces.
- **Never:** the actions the poster ruled out.
- **Context events:** the summaries of the notes linked with `context_event_ids`.
- **Project:** when `project_id` is set, that project's saved summary, next action, decisions and blockers from `save_work_session`. A warning is returned when the project does not exist in this deployment.

## Rules the board enforces

- **One holder at a time.** The claim is decided inside a single SQLite write transaction, so two agents asking for the same role at the same moment never receive the same task.
- **Leases.** A claim lasts `lease_minutes` (1 to 1440, default 120). After that the task is open again, so an agent that crashed or lost its session does not block the queue. The old claim can no longer settle the task once another agent has claimed it.
- **Only the holder settles.** `settle_task` checks both `claim_id` and `agent`.
- **Checkable proof for done.** Each proof item must be a URL, `event:<id>` naming an event in this deployment, a commit hash, or a file path or ref such as `owner/repo#4`. Free text such as "did it" is refused. The board checks that proof can be followed, not that it is correct; that is what verification is for.
- **Done is not verified.** `verify_task` must come from a different agent than the one that settled. Verification is advisory, except for tasks posted with `require_verified`, which wait for it. Agent names are self-declared, so this separates roles you set up, not untrusted parties.
- **Dependencies.** A task with `depends_on` stays `waiting` until every upstream task is `done` (and verified, if required). If an upstream task is blocked, failed or rejected, the dependent shows as `stuck`, with `stuck_on` naming the cause.
- **Follow-ups.** Post a new task with `followup_to=<stuck cause>`. Tasks that depended on the old one now wait on the follow-up, and the follow-up's briefing carries the old outcome. Settled tasks themselves are never edited.
- **Write policy.** Briefs and outcomes go through the same secret and size filter as every other write.

## How it is stored

Each post, claim, renewal, settlement and verification is one append-only event with `source = "tasks"` and type `task.posted`, `task.claimed`, `task.renewed`, `task.settled` or `task.verified`. The board is rebuilt from those events on every read, so there is no second table to drift out of sync, and tasks appear in `search_events` and the browser search like any other note. `actor` is the task id, so `query_events(source="tasks", actor="t_...")` returns one task's full history.

## Choosing roles

A role is a short lowercase name (`research`, `build`, `review`, `writer`, `social`, `analytics`). Pick roles by the kind of work and the tools an agent needs, not by which model runs it. That way you can swap the agent behind a role without reposting anything. A common split:

| Role | Takes | Typical proof |
|---|---|---|
| `lead` | Breaks goals into tasks, reviews results, posts the next task | the follow-up task ids |
| `research` | Questions with a written answer: competitors, sources, lists | a note event id with source links |
| `build` | Code, sites, scripts, fixes | commit or pull request URL, live URL |
| `writer` | Drafts: posts, emails, docs | document id and revision |
| `review` | Checks another role's result against its `done_when` | a note event id with the verdict |

Keep a person in the loop for anything that sends, publishes or spends. The board records what agents reported and checked; it does not verify deliveries itself or authorize external actions. Post work that builds on a result with `require_verified=true`, and have your `review` agent verify, to get a second check before anything acts on it.

## A starting prompt for a role agent

Give each agent its role and the loop. For example:

```
You work the "research" role on an Open Brane task board (MCP server "open-brane").
1. Call role_brief(role="research") once at the start of a session.
2. Call claim_task(role="research", agent="<your name>"). If task is null, stop.
3. Read the brief, done_when and briefing. Do the work.
   Read briefing.already_known first and do not redo work that is already saved.
   Respect briefing.never. For long work, call renew_claim before the lease ends.
4. Save your result with record_event (include a summary), then call settle_task with
   status "done" and proof such as ["event:<id>", "<source URL>"], plus a lesson
   if you learned something the next agent should know.
   If you cannot finish, settle as "blocked" and name exactly what is missing.
5. Claim the next task, or stop when there is none.

For the review role, replace steps 2-4 with: call role_brief for the role you
check, open each task under awaiting_verification, follow its proof, and call
verify_task with "verified" or "rejected" and a note saying what you checked.
```

Agents on different machines and harnesses can share one board by connecting to the same deployment over HTTP MCP. One deployment is one trusted owner: every connected agent has full access, so give separate trust boundaries separate deployments.
