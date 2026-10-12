"""Optional existing-CRM facts. Event history remains OpenBrane's authority.

Providers never send and never write CRM or event records. A provider result is
an independently fetched contact snapshot, not payment or delivery evidence.
"""
from __future__ import annotations

import importlib
import json
import os
import re
from urllib.parse import urlparse

import httpx


class CRMProviderError(RuntimeError):
    """Safe provider failure without endpoints, credentials or response bodies."""


def _env_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        raise CRMProviderError("provider credentials must use environment variable names")
    return value


def validate_facts(facts, business_id):
    if facts is None:
        return None
    if not isinstance(facts, dict) or facts.get("business_id") != business_id:
        raise CRMProviderError("provider returned a contact outside the configured business")
    return facts


class SupabaseFacts:
    """Read existing crm_contacts using an explicit business filter on every call."""

    def __init__(self, options=None, *, transport=None):
        options = options or {}
        if set(options) - {"endpoint_env", "credential_env", "timeout_seconds"}:
            raise CRMProviderError("unsupported Supabase options; use endpoint_env and credential_env")
        endpoint_env = _env_name(options.get("endpoint_env", "SUPABASE_URL"))
        credential_env = _env_name(options.get("credential_env", "SUPABASE_SERVICE_KEY"))
        endpoint = os.environ.get(endpoint_env, "").rstrip("/")
        credential = os.environ.get(credential_env, "")
        parsed = urlparse(endpoint)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise CRMProviderError("Supabase endpoint must be configured as an HTTP origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise CRMProviderError("remote Supabase endpoints require HTTPS")
        if not credential:
            raise CRMProviderError("Supabase credential environment variable is missing")
        timeout = options.get("timeout_seconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 60:
            raise CRMProviderError("provider timeout_seconds must be between 0 and 60")
        self.endpoint = endpoint
        self.client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False,
                                   headers={"apikey": credential, "Authorization": "Bearer " + credential})

    def contact_facts(self, business_id, *, email=None, phone=None):
        if bool(email) == bool(phone):
            raise CRMProviderError("provider lookup requires exactly one email or phone")
        params = {"select": "id,business_id,email,phone,lifecycle_status,last_inbound_at,last_outbound_at,updated_at",
                  "business_id": "eq." + business_id, "limit": "2"}
        params["email" if email else "phone"] = "eq." + (email or phone)
        try:
            response = self.client.get(self.endpoint + "/rest/v1/crm_contacts", params=params)
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError):
            raise CRMProviderError("CRM provider lookup failed") from None
        if not isinstance(rows, list) or len(rows) > 1:
            raise CRMProviderError("CRM provider returned an invalid or ambiguous contact result")
        return validate_facts(rows[0], business_id) if rows else None

    def close(self):
        self.client.close()


def load_provider():
    """Use configured facts provider. Custom factories are trusted owner code."""
    name = os.environ.get("CRM_PROVIDER", "event")
    try:
        options = json.loads(os.environ.get("CRM_PROVIDER_OPTIONS_JSON", "{}"))
    except (ValueError, TypeError):
        raise CRMProviderError("CRM_PROVIDER_OPTIONS_JSON must be a JSON object") from None
    if not isinstance(options, dict):
        raise CRMProviderError("CRM provider options must be a JSON object")
    for key in options:
        if any(secret in key.lower() for secret in ("secret", "token", "password", "api_key")) and not key.endswith("_env"):
            raise CRMProviderError("literal credentials are not accepted in provider options")
    if name == "event":
        if options:
            raise CRMProviderError("event provider has no options")
        return None
    if name == "supabase":
        return SupabaseFacts(options)
    if ":" not in name:
        raise CRMProviderError("unknown CRM provider")
    module, factory_name = name.rsplit(":", 1)
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) or not re.fullmatch(r"[A-Za-z_]\w*", factory_name):
        raise CRMProviderError("custom provider must be module:factory")
    try:
        provider = getattr(importlib.import_module(module), factory_name)(options)
    except Exception:
        raise CRMProviderError("custom CRM provider could not initialize") from None
    if not callable(getattr(provider, "contact_facts", None)):
        raise CRMProviderError("custom provider must expose contact_facts")
    return provider
