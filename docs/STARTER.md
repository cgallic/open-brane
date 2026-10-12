# Copy Open Brane into another system

Open Brane's starter runs the existing event writer and MCP server from a deployment JSON file. Contacts, deals, and reported interactions live in the same event log. The optional CRM provider reads contact facts from another system; it does not replace the event store or send messages.

## Install and initialize

Use Python 3.11+ and install `requirements.txt` in a virtual environment. Follow the platform commands in [README.md](../README.md), then copy `config/starter.example.json` to `config/starter.json`. Set a business identifier before initializing:

```json
{
  "version": 1,
  "business_id": "your-business",
  "database": "../data/events.db",
  "policy_mode": "strict",
  "vectors": {"enabled": false},
  "crm": {"provider": "event", "options": {}}
}
```

From the package directory, with the environment active:

```text
python scripts/starter.py validate --config config/starter.json
python scripts/starter.py init --config config/starter.json
python scripts/starter.py doctor --config config/starter.json
```

`validate` checks configuration structure and reports the resolved database path. It does not contact providers. `init` creates the database parent directory and applies the event, governance, recall-trace, and edge schemas. Repeating it preserves events. `doctor` checks database integrity and reports vector services as disabled or checks them when enabled. It does not verify CRM credentials or contact facts.

`run` starts stdio MCP. A terminal waiting for input is expected:

```text
python scripts/starter.py run --config config/starter.json
```

## Connect your MCP client

Use absolute paths so client launch does not depend on its working directory. Replace these example paths with the location of your copy:

```json
{
  "mcpServers": {
    "open-brane": {
      "command": "/absolute/path/open-brane/.venv/bin/python",
      "args": [
        "/absolute/path/open-brane/scripts/starter.py",
        "run",
        "--config",
        "/absolute/path/open-brane/config/starter.json"
      ]
    }
  }
}
```

On Windows use the environment's `Scripts/python.exe`, for example `C:/Projects/open-brane/.venv/Scripts/python.exe`, and corresponding absolute script/config paths. Forward slashes work in JSON and avoid backslash escaping. Your client may use another server configuration format; keep the same command and arguments. The server and its child scripts use that Python environment.

## Configuration contract

Unknown top-level configuration keys are rejected. Database paths resolve relative to the configuration file, not the shell directory. `config/starter.schema.json` describes the shape; `runtime_config.py` performs runtime validation without requiring a JSON Schema library.

| JSON setting | Existing environment variable | Meaning |
|---|---|---|
| `business_id` | `CRM_BUSINESS_ID` | Required CRM business binding for this deployment |
| `database` | `BRAIN_DB` | Required absolute or config-relative SQLite path |
| `policy_mode` | `BRAIN_POLICY_MODE` | `strict` or `sanitize`; default `strict` |
| `vectors.enabled` | `BRAIN_VECTORS_ENABLED` | Default `false`; semantic retrieval unavailable when disabled |
| `vectors.ollama_url` | `OLLAMA_URL` | Ollama-compatible embedding endpoint origin; default local port 11434 |
| `vectors.qdrant_url` | `QDRANT_URL` | Qdrant endpoint; default local port 6333 |
| `vectors.model` | `EMBED_MODEL` | Default `nomic-embed-text` |
| `vectors.dimensions` | `EMBED_DIMENSIONS` | Positive integer; default 768; must match model output |
| `vectors.collection` | `BRAIN_COLLECTION` | Default `brain_events`; choose a fresh collection when changing models |
| `crm.provider` | `CRM_PROVIDER` | `event`, `supabase`, or an importable `module:factory` |
| `crm.options` | `CRM_PROVIDER_OPTIONS_JSON` | Provider options; reference credential environment names |

The starter sets `BRAIN_ROOT` to its package directory. These variables are passed to existing scripts by `run`; they are not exported back into your shell. Calling an adapter or another script directly requires the same environment settings. Configuration values override existing environment values for the variables in this table.

Optional inherited CRM settings are `CRM_CONTACT_TTL_HOURS` (default 2) and `CRM_DORMANT_DAYS` (default 21). Set them in the environment that launches MCP. Each `check_outreach` call can supply `cooldown_days` (default 7). Timestamps must explicitly use UTC.

## Brain and CRM tools

The existing tools remain available: `record_event`, `query_events`, `semantic_search`, `report_usage`, `propose_edge`, and `health_check`. `search_events` adds literal text search over event payloads without a vector service. A disabled or failing semantic backend returns `status: backend_unavailable`; inspect that status instead of treating an empty result list as a successful search. With vectors disabled, the MCP `health_check` tool checks the event log and reports `scope: core`; starter `doctor` also runs a SQLite integrity check. Neither verifies CRM contact facts.

| CRM tool | Behavior |
|---|---|
| `upsert_contact` | Append a complete contact snapshot with identity, kind, contact policy, and expiry |
| `find_contact` | Read current local contact snapshots by ID, normalized email, exact E.164 phone, or name |
| `upsert_deal` | Append a deal snapshot referencing an existing contact in the same business |
| `get_pipeline` | Read latest local deal snapshots; deal values are not collected revenue |
| `log_outreach` | Append a reported inbound or outbound interaction with a required `message_id` |
| `outreach_history` | Read reported interactions with contact, mailbox, agent, or time filters |
| `check_outreach` | Combine local policy/history with optional external contact facts; return a verdict |

All CRM writes use `record_event.append_event`; current records are projections of append-only snapshots. An upsert is a replacement snapshot, so supply every contact/deal field you want retained. Contacts and deals do not automatically write to external CRM providers.

For a first local MCP read-back, call these tools with the shown arguments:

```json
{
  "tool": "upsert_contact",
  "arguments": {
    "contact_id": "demo-contact",
    "email": "demo@example.com",
    "display_name": "Demo contact",
    "kind": "test",
    "contact_policy": "unknown"
  }
}
```

```json
{"tool": "find_contact", "arguments": {"query": "demo-contact"}}
```

```json
{
  "tool": "upsert_deal",
  "arguments": {
    "deal_id": "demo-deal",
    "contact_id": "demo-contact",
    "title": "Demo opportunity",
    "stage": "open"
  }
}
```

```json
{"tool": "get_pipeline", "arguments": {}}
```

These are tool names and arguments for your client, rather than raw JSON-RPC envelopes. The demo records append to your local database. A `test` contact is excluded from outreach.

CRM calls reject a supplied business ID that differs from configuration. `check_outreach` returns `caution` for unknown, ambiguous, stale, held, or unavailable facts; excluded kinds and do-not-contact policy return `do_not_contact`. A clear verdict still includes `send_authorized: false`. Sending requires a separate provider and authorization outside this package.

`log_outreach` records an observation with `observation_status: reported`, `sent: false`, and `delivery_verified: false`. Its retry identity includes business, direction, channel, message ID, and contact. Exact retries return the same event ID; reusing the identity with changed contents is rejected. A message ID supplied by a caller is not independently authenticated delivery proof.

The canonical event writer also exposes `--dedupe-key` to adapters. Keys are scoped by source and bind to canonical event content. Legacy calls without a key append again. Keep explicit observation timestamps stable across retries.

## Replace CRM providers

`event` is the default and accepts no options. It uses local contacts and interaction history. External providers add read-only facts during `check_outreach`; populate the local contact first with `upsert_contact`.

For an existing Supabase CRM:

```json
{
  "provider": "supabase",
  "options": {
    "endpoint_env": "MY_CRM_URL",
    "credential_env": "MY_CRM_KEY",
    "timeout_seconds": 10
  }
}
```

Use that object as `crm` in the deployment configuration. Set `MY_CRM_URL` to your Supabase origin and `MY_CRM_KEY` to a credential with access to the required contact rows before starting MCP. The defaults are `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`. No URL or credential is embedded in the package. Remote endpoints require HTTPS; local development origins may use HTTP. Redirects are not followed.

The adapter issues GET requests to `/rest/v1/crm_contacts` with an exact `business_id` filter and either email or phone. Your existing table must expose `id`, `business_id`, `email`, `phone`, `lifecycle_status`, `last_inbound_at`, `last_outbound_at`, and `updated_at`. It reads at most two rows so ambiguous identities can be rejected. It does not create tables, migrate a provider database, or perform writes. Use a custom adapter if your CRM has different columns or identity rules.

For another CRM set `crm.provider` to `my_crm:create_provider`. Put `my_crm.py` on the MCP process's Python import path, for example inside this copy's `scripts/` directory, or install your adapter in its environment. The factory receives the options dictionary and returns an object with:

```python
def contact_facts(self, business_id, *, email=None, phone=None):
    # Fetch exactly one identity within business_id.
    # Return None when there is no match, or the contact facts below.
    ...

def close(self):
    # Optional: release provider connections.
    ...
```

A matched result must be a dictionary with the exact requested `business_id` and identity. Supply `lifecycle_status`, UTC `updated_at`, and optional UTC `last_inbound_at` / `last_outbound_at` for the outreach check. Accepted external lifecycle values are `lead`, `prospect`, `contact`, `partner`, and `churned`; excluded kinds return do-not-contact, and other values return caution. A missing match also returns caution. Raise on failed or ambiguous lookups. Keep reads business-scoped and credentials in environment variables. Custom factories are trusted owner code and can execute Python; they are not sandboxed plugins.

## Enable semantic search

Install and run Ollama and Qdrant separately, load the selected embedding model, then set `vectors.enabled` to `true` and configure their endpoints. The included clients use Ollama-compatible embedding calls and the Qdrant API. Changing endpoints is supported; changing the vector engine requires code.

Index events using the same configuration, from the package directory:

```text
python scripts/starter.py embed --config config/starter.json
```

Re-running the embedder skips indexed event IDs. Match `dimensions` to your embedding model; switching models should use a new collection and re-index all events. Check the embedder's `errors` count and starter `doctor`, then verify an actual `semantic_search` response. Health alone does not prove indexed retrieval.

## Copy, verify, and keep data private

Copy the public source or clone the repository into the destination system. Install its requirements, make a new `starter.json`, and point it at a separate database. Run `validate`, `init`, `doctor`, connect MCP, and read back a test contact and pipeline entry. Provider credentials and custom modules belong to that destination. Run the included checks with `python -m unittest discover -s tests -v`.

One installation is for one trusted owner/operator. General event tools read the entire configured database, so CRM business filters do not provide multi-user Brain isolation. Use separate databases and access controls for different organizations. The stdio transport inherits access from the launching process; the optional HTTP/SSE template is not a public authenticated API.

Keep `data/`, database files, local configuration, credentials, external snapshots, and rendered private views outside your shared package. Back up SQLite with a consistent snapshot and preserve any externally referenced attachments. Derived vectors and views can be rebuilt from events; losing the event database loses their source.
