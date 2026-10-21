# Customer handoff: phone workflow to follow-up workflow

Open Brane keeps recorded customer interactions available to different workflows. A follow-up client can read the contact snapshot, recent interactions, source event IDs, freshness, and reasons to pause in one call. The browser's Customer desk shows the same data.

This integration is owner-controlled. HTTP callers use the deployment's full-owner Bearer token; use separate deployments for different customers or trust boundaries. Keep that token on a trusted automation server, never in a public browser, customer-facing form, or an untrusted agent's configuration. These endpoints are not a public provider webhook receiver and do not validate ElevenLabs, Vapi, or email-provider signatures. Authenticate provider events in your own adapter before forwarding normalized observations.

## Run the two-client proof

After normal setup:

```bash
python3 brane.py demo
```

This starts two **scripted MCP clients**, representing voice and follow-up workflows, against a temporary, isolated deployment. It does not invoke an LLM, contact a provider, or send any message. Every check uses the real MCP transport and tools:

1. Voice client records an inbound quote request; both clients read the same event IDs and summary.
2. Retrying the interaction produces the original event ID, with `deduped: true`.
3. Follow-up client records an outbound observation; the voice client sees the recent-outreach caution.
4. A contact reclassified as an existing customer is excluded from proactive outreach.
5. An expired policy snapshot causes a caution.
6. Both clients read document revision 1. One updates it to revision 2. The other's stale revision-1 edit is rejected; both revisions remain available.

The command exits nonzero if a check fails and emits a JSON report on success. It deletes its temporary data when it finishes. `messages_sent` is always zero.

## Connect an existing workflow

Start the normal server:

```bash
python3 brane.py serve
```

From a second terminal, push the provided synthetic call example with a Python standard-library client:

```bash
python3 examples/push_customer_handoff.py --token-file data/http-token
```

The example uses `examples/customer-handoff.json` and deliberately sets contact policy to `unknown`. Open the browser, unlock it, and look up `demo-lead` in Customer desk. The inbound summary will appear with a caution until the owner supplies a fresh, trusted policy snapshot. Repeating the example appends another contact snapshot, while its interaction remains deduplicated.

For your own normalized data:

```bash
python3 examples/push_customer_handoff.py --url https://brain.example.com --token-file /private/http-token --input /private/customer-handoff.json
```

The example client refuses remote plaintext HTTP and HTTP redirects to avoid forwarding owner credentials. It creates/updates the supplied contact snapshot, records the interaction, then reads context. Those are separate requests: if the interaction fails, the contact snapshot may already exist. Retry with the same interaction identity and exact contents. This example is an adapter contract, not a production connector for a specific CRM or voice provider.

## HTTP contract

Every route below requires `Authorization: Bearer YOUR_ACCESS_TOKEN`. Use JSON bodies and HTTPS for remote deployments. Bodies are limited to 256 KiB; summaries must satisfy the existing write policy, including its 500-character summary limit. Missing/invalid tokens return 401. Unknown request fields are rejected, including caller-supplied business IDs and database paths.

### POST /api/contacts

Create a **complete contact snapshot**, not a patch:

```json
{
  "contact_id": "lead-123",
  "email": "alex@example.invalid",
  "phone": "+12025550123",
  "display_name": "Alex",
  "kind": "lead",
  "contact_policy": "unknown"
}
```

Supported fields: `contact_id` (required), `email`, `phone`, `display_name`, `kind`, `contact_policy`, `observed_at`, `valid_until`. Timestamps must be explicit UTC; observation time cannot be in the future. Existing CRM validation applies. Omitted email/phone fields disappear from the new snapshot, so forward a complete record. The deployment assigns `business_id` server-side.

`contact_policy` defaults to `unknown`; do not let an LLM infer `allowed` from a conversation. An owner/trusted policy source may set `allowed`, `hold`, or `do_not_contact`. By default snapshots expire after two hours (`CRM_CONTACT_TTL_HOURS`). Recording an interaction does not refresh policy. This policy is a local workflow fact, not legal consent or authority to send.

### POST /api/interactions

```json
{
  "contact": "lead-123",
  "mailbox": "office-phone-line",
  "agent": "voice-workflow",
  "channel": "call",
  "direction": "inbound",
  "message_id": "voice-provider:conversation-456",
  "summary": "Alex requested a quote and a callback tomorrow."
}
```

Required fields: `contact`, `mailbox`, `agent`, `message_id`. Optional: `channel` (default `email`), `direction` (default `outbound`), `subject`, `thread_id`, `ts`, `summary`. Supply direction explicitly for inbound events. Resolve a known contact by ID, exact email, E.164 phone, or an unambiguous name; otherwise recording fails. Use contact IDs or exact identities in production.

Use a stable, provider-namespaced `message_id`. Retry identity is scoped by deployment business, resolved contact ID, direction, channel, and message ID. A first write returns 201. An exact retry returns 200 and the same `event_id`. Changing the payload under that identity returns 409 and preserves the original event. Supply the original provider timestamp or omit `ts`; do not generate a new timestamp for each retry. Distinct contacts or channels have distinct identities.

A policy rejection returns 422, invalid fields return 400, and a SQLite failure returns 503. Success means an observation was recorded. Responses explicitly set `sent: false` and `delivery_verified: false`; a caller cannot elevate these by adding request fields. The summary and agent label are caller-reported. Provider receipt validation and CRM synchronization belong in the adapter.

### GET /api/customer-context?contact=lead-123

URL-encode contact values, including `%2B` for a phone number’s leading plus sign. Unknown query parameters are rejected. Optional query parameters: `limit` (1–500; default 20) and `cooldown_days` (nonnegative integer; default 7).

Returns:

- `status`: `found`, `unknown`, or `ambiguous`. Unknown/ambiguous queries return no contact or timeline.
- `contact`: latest recorded contact snapshot, its source event ID, and `policy_fresh`.
- `decision`: existing outreach checks, including `verdict`, `reason`, and `whose_turn`.
- `timeline` / `latest_interaction`: most recent reported interactions, including summaries, agent labels, timestamps, message IDs, and source event IDs.
- `evidence`: contact and displayed interaction event IDs.
- `checked_at`, `timeline_limit`, and `history_scope: recorded_interactions_only`.
- `send_authorized: false`.

The local contact facts, history, and decision are read from **one SQLite transaction snapshot**. A configured external CRM provider is queried separately and may cause the check to pause if unavailable or stale. Local event IDs do not prove the authenticity of externally reported facts.

An empty timeline does not prove no interaction occurred. Only instrumented, successfully recorded events are visible; recent results are limited. A clear result is not an atomic reservation. Another client can act immediately after this read. Your sending workflow must still enforce authorization, suppression, concurrency control, and receipt handling. These endpoints do not send, schedule, reserve, or verify delivery.

Existing customers are excluded from **proactive outreach** by the existing CRM rules. This is not a rule for suppressing requested customer-support replies; use a separate, explicitly authorized service workflow.

## MCP equivalent

Clients can call `upsert_contact`, `log_outreach` (now accepting an optional `summary`), and `customer_context`. Existing `log_outreach` calls retain their previous default summary and retry behavior. The browser, HTTP API, and MCP tools all use the canonical event writer; no new event database or second write path is introduced.

## Operational limits

The current CRM derives state by scanning recorded CRM events. This release makes no large-scale performance claim. The demo proves specific transport, storage, and conflict behaviors with synthetic inputs; it does not establish provider integration reliability, better model recall, or customer ROI. Offsite backups and monitoring remain deployment responsibilities.
