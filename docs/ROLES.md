# Hand work between agents by role

When several agents work for you, each usually has a job: one researches, one writes code, one drafts posts, one checks the others. The role task board gives that split a shared place to live. A lead (a person or an agent) posts a task for a role. Whichever agent works that role claims it, gets the context it needs to start, and settles it with proof. The next task in the chain then starts from that result.

Try it with scripted clients and temporary data:

```bash
python3 brane.py demo roles
```

```
lead posted t_072d6b4af9e5 for research and t_d7efd8ebc25c for build
builder asked for work: nothing ready, 1 task waiting on research
researcher claimed t_072d6b4af9e5 until 2026-11-27T13:13Z
settling as done without proof: refused
researcher settled done with proof event:4
builder claimed t_d7efd8ebc25c and received: "Five questions saved as a note." (proof event:4)
board: build=claimed, research=done
```

The three clients are scripts, not autonomous agents. Nothing is sent and the data is deleted when the demo ends.

## The tools

| Tool | What it does |
|---|---|
| `post_task(role, title, brief, done_when, posted_by?, project_id?, depends_on?, context_event_ids?, request_id?)` | Posts a task for one role. `done_when` is the line the result is judged against. `depends_on` lists task ids that must be done first. `project_id` links a saved project. `context_event_ids` points at notes the agent should read. A retried post with the same `request_id` returns the same task. |
| `claim_task(role, agent, lease_minutes=120, task_id?)` | Claims the oldest ready task for the role and returns it with a briefing. Returns `task: null` when nothing is ready, with a count of tasks waiting on dependencies. |
| `settle_task(task_id, claim_id, agent, status, outcome, proof?)` | `status` is `done`, `blocked` or `failed`. `done` needs at least one proof item: a URL, commit, file path or event id. For `blocked` and `failed`, say in `outcome` exactly what is missing. |
| `list_tasks(role?, status?, limit?)` | The board, newest first. Status is `open`, `waiting`, `claimed`, `done`, `blocked` or `failed`. |
| `role_brief(role)` | What a fresh agent in a role reads first: its open queue, active claims, waiting tasks and recent results. |

The browser home screen shows the same board (read-only), and `python3 brane.py tasks [--role build] [--status open]` prints it in the terminal. Over HTTP the board is `GET /api/tasks` with the Bearer token.

## What the briefing contains

`claim_task` returns saved facts only. It never guesses at progress.

- **Recent outcomes for the role:** the last five settled tasks of that role, with outcome and proof. An agent sees what the previous agent in its seat found or got stuck on.
- **Upstream results:** for each task this one depends on, its outcome and proof. This is the handoff: the build agent starts from the research agent's result, not from a summary someone retyped.
- **Context events:** the summaries of the notes linked with `context_event_ids`.
- **Project:** when `project_id` is set, that project's saved summary, next action, decisions and blockers from `save_work_session`. A warning is returned when the project does not exist in this deployment.

## Rules the board enforces

- **One holder at a time.** The claim is decided inside a single SQLite write transaction, so two agents asking for the same role at the same moment never receive the same task.
- **Leases.** A claim lasts `lease_minutes` (1 to 1440, default 120). After that the task is open again, so an agent that crashed or lost its session does not block the queue. The old claim can no longer settle the task once another agent has claimed it.
- **Only the holder settles.** `settle_task` checks both `claim_id` and `agent`.
- **Proof for done.** A task cannot be marked done with an empty proof list.
- **Dependencies.** A task with `depends_on` stays `waiting` until every upstream task is `done`. A blocked or failed upstream task keeps it waiting; post a new task to retry.
- **Settled is final.** To redo work, post a new task. History stays intact.
- **Write policy.** Briefs and outcomes go through the same secret and size filter as every other write.

## How it is stored

Each post, claim and settlement is one append-only event with `source = "tasks"` and type `task.posted`, `task.claimed` or `task.settled`. The board is rebuilt from those events on every read, so there is no second table to drift out of sync, and tasks appear in `search_events` and the browser search like any other note. `actor` is the task id, so `query_events(source="tasks", actor="t_...")` returns one task's full history.

## Choosing roles

A role is a short lowercase name (`research`, `build`, `review`, `writer`, `social`, `analytics`). Pick roles by the kind of work and the tools an agent needs, not by which model runs it. That way you can swap the agent behind a role without reposting anything. A common split:

| Role | Takes | Typical proof |
|---|---|---|
| `lead` | Breaks goals into tasks, reviews results, posts the next task | the follow-up task ids |
| `research` | Questions with a written answer: competitors, sources, lists | a note event id with source links |
| `build` | Code, sites, scripts, fixes | commit or pull request URL, live URL |
| `writer` | Drafts: posts, emails, docs | document id and revision |
| `review` | Checks another role's result against its `done_when` | a note event id with the verdict |

Keep a person in the loop for anything that sends, publishes or spends. The board records what an agent reported; it does not verify deliveries or authorize external actions. A `review` task that depends on a `build` task is a simple way to get a second check before you act.

## A starting prompt for a role agent

Give each agent its role and the loop. For example:

```
You work the "research" role on an Open Brane task board (MCP server "open-brane").
1. Call role_brief(role="research") once at the start of a session.
2. Call claim_task(role="research", agent="<your name>"). If task is null, stop.
3. Read the brief, done_when and briefing. Do the work.
4. Save your result with record_event (include a summary), then call settle_task with
   status "done" and proof such as ["event:<id>", "<source URL>"].
   If you cannot finish, settle as "blocked" and name exactly what is missing.
5. Claim the next task, or stop when there is none.
```

Agents on different machines and harnesses can share one board by connecting to the same deployment over HTTP MCP. One deployment is one trusted owner: every connected agent has full access, so give separate trust boundaries separate deployments.
