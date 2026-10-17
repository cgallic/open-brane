# Portable customer runtime

The starter now supports one isolated customer instance per configuration. Set `owner_id`, `public_origin`, and the `storage` paths in `starter.json`; `init` creates the event database plus a separate auth/artifact database and artifact root. Vector collection names are configured independently and lexical `search_events` remains available when vectors are disabled.

Customer connections are created inside the instance and receive a server-generated `connection_id`. The runtime stamps that identity on connection-aware event writes, binds checkpoints to the configured owner, and rejects missing, revoked, read-only, or foreign connections. Provider credentials and personal Brain identities are not included.

Artifacts are UTF-8 documents up to 256 KiB. Slugs are containment-checked, writes use a per-artifact lock and atomic replacement, and each edit creates an immutable revision with SHA-256, provenance, parent revision, and connection identity. Edits require `expected_revision`; stale writers fail without changing the current revision. History, restore, and export are available through the MCP tools.

This is the portable foundation, not the complete product portal. Hosted Claude/ChatGPT OAuth UI, browser passkey enrollment, website editing/rendering/publication, billing, offsite serving, and customer onboarding remain open work.
