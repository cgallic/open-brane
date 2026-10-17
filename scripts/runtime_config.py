"""Load a portable Open Brane configuration into its existing environment API."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path) -> tuple[dict, dict[str, str]]:
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    allowed = {"version", "business_id", "owner_id", "public_origin", "database", "storage", "auth", "policy_mode", "vectors", "crm"}
    if set(config) - allowed:
        raise ValueError(f"unknown configuration keys: {sorted(set(config) - allowed)}")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("version must be 1")
    business = config.get("business_id")
    if not isinstance(business, str) or not business.strip():
        raise ValueError("business_id must be a nonempty deployment identifier")
    owner = config.get("owner_id", business)
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 200 or any(ch in owner for ch in "\\/:\x00"):
        raise ValueError("owner_id must be a safe nonempty customer identifier")
    origin = config.get("public_origin", "http://127.0.0.1")
    parsed_origin = urlparse(origin) if isinstance(origin, str) else None
    if not parsed_origin or parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname or parsed_origin.username or parsed_origin.password or parsed_origin.query or parsed_origin.fragment:
        raise ValueError("public_origin must be an HTTP(S) origin without credentials or query parameters")
    database = config.get("database")
    if not isinstance(database, str) or not database.strip():
        raise ValueError("database must be a path")
    db = Path(database)
    db = db.resolve() if db.is_absolute() else (path.parent / db).resolve()
    storage = config.get("storage", {})
    if not isinstance(storage, dict) or set(storage) - {"root", "auth_db", "artifact_root", "vector_namespace"}:
        raise ValueError("storage contains unsupported options")
    def resolve_storage(name, default):
        value = storage.get(name, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"storage.{name} must be a path or namespace")
        if name == "vector_namespace":
            if any(ch in value for ch in "\\/\x00"):
                raise ValueError("storage.vector_namespace must be a portable namespace")
            return value
        candidate = Path(value)
        return str((candidate if candidate.is_absolute() else path.parent / candidate).resolve())
    storage_root = resolve_storage("root", "data")
    auth_db = resolve_storage("auth_db", str(Path(storage_root) / "auth.db"))
    artifact_root = resolve_storage("artifact_root", str(Path(storage_root) / "artifacts"))
    vector_namespace = resolve_storage("vector_namespace", f"brain_{business}")
    auth = config.get("auth", {})
    if not isinstance(auth, dict) or set(auth) - {"enabled", "read_only_default"}:
        raise ValueError("auth contains unsupported options")
    if not isinstance(auth.get("enabled", True), bool) or not isinstance(auth.get("read_only_default", False), bool):
        raise ValueError("auth.enabled and auth.read_only_default must be boolean")
    policy = config.get("policy_mode", "strict")
    if policy not in {"strict", "sanitize"}:
        raise ValueError("policy_mode must be strict or sanitize")
    vectors = config.get("vectors", {})
    if not isinstance(vectors, dict) or set(vectors) - {"enabled", "ollama_url", "qdrant_url", "model", "dimensions", "collection"}:
        raise ValueError("vectors contains unsupported options")
    enabled = vectors.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("vectors.enabled must be boolean")
    dimensions = vectors.get("dimensions", 768)
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
        raise ValueError("vectors.dimensions must be a positive integer")
    model = vectors.get("model", "nomic-embed-text")
    collection = vectors.get("collection", "brain_events")
    if not all(isinstance(value, str) and value.strip() for value in (model, collection)):
        raise ValueError("vectors.model and vectors.collection must be nonempty strings")
    urls = {}
    for name, default in (("ollama_url", "http://127.0.0.1:11434"), ("qdrant_url", "http://127.0.0.1:6333")):
        value = vectors.get(name, default)
        parsed = urlparse(value) if isinstance(value, str) else None
        if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"vectors.{name} must be an HTTP URL without credentials")
        urls[name] = value.rstrip("/")
    crm = config.get("crm", {})
    if not isinstance(crm, dict) or set(crm) - {"provider", "options"}:
        raise ValueError("crm contains unsupported options")
    provider = crm.get("provider", "event")
    if not isinstance(provider, str) or not provider:
        raise ValueError("crm.provider must be a provider name")
    if provider not in {"event", "supabase"}:
        parts = provider.split(":")
        if len(parts) != 2 or not all(part.isidentifier() for part in parts[0].split(".")) or not parts[1].isidentifier():
            raise ValueError("crm.provider must be event, supabase, or python_module:factory")
    options = crm.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("crm.options must be an object; credentials belong in environment variables")
    for key in options:
        if any(secret in key.lower() for secret in ("secret", "token", "password", "api_key")) and not key.endswith("_env"):
            raise ValueError("crm.options must reference credential environment variables, not literal credentials")
    if provider == "event" and options:
        raise ValueError("event CRM provider does not accept options")
    if provider == "supabase" and set(options) - {"endpoint_env", "credential_env", "timeout_seconds"}:
        raise ValueError("unsupported Supabase options")
    env = {
        "BRAIN_ROOT": str(ROOT), "BRAIN_DB": str(db), "BRAIN_POLICY_MODE": policy,
        "BRAIN_OWNER_ID": owner, "BRAIN_PUBLIC_ORIGIN": origin.rstrip("/"),
        "BRAIN_STORAGE_ROOT": storage_root, "BRAIN_AUTH_DB": auth_db,
        "BRAIN_ARTIFACT_ROOT": artifact_root, "BRAIN_VECTOR_NAMESPACE": vector_namespace,
        "BRAIN_AUTH_ENABLED": "true" if auth.get("enabled", True) else "false",
        "BRAIN_READ_ONLY_DEFAULT": "true" if auth.get("read_only_default", False) else "false",
        "BRAIN_VECTORS_ENABLED": "true" if enabled else "false",
        "OLLAMA_URL": urls["ollama_url"], "QDRANT_URL": urls["qdrant_url"],
        "EMBED_MODEL": model, "EMBED_DIMENSIONS": str(dimensions), "BRAIN_COLLECTION": collection,
        "CRM_BUSINESS_ID": business, "CRM_PROVIDER": provider,
        "CRM_PROVIDER_OPTIONS_JSON": json.dumps(options),
    }
    return config, env


def apply_config(path: str | Path) -> dict:
    config, env = load_config(path)
    os.environ.update(env)
    return config
