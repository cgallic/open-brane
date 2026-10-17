PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, provider TEXT NOT NULL,
    label TEXT NOT NULL, scopes_json TEXT NOT NULL, read_only INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT, created_at TEXT NOT NULL, last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    project_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    connection_id TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL, current_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_revisions (
    artifact_id TEXT NOT NULL, revision_id INTEGER NOT NULL, parent_revision INTEGER,
    sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL, path TEXT NOT NULL,
    provenance_json TEXT NOT NULL, created_at TEXT NOT NULL, connection_id TEXT NOT NULL,
    PRIMARY KEY (artifact_id, revision_id), FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);
