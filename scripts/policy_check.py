#!/usr/bin/env python3
"""
policy_check.py — unsafe-write filter for events.

Importable module. Inspects a record_event payload BEFORE it's written and
rejects (or sanitizes) entries that violate basic safety policies. Stops
agents from dumping raw transcripts, API keys, or 50 KB of code into the
brain, where they'd just pollute embeddings and retrieval.

Usage from record_event.py:

    from policy_check import check, PolicyError

    try:
        payload = check(source=args.source, etype=args.etype, payload_raw=payload_str)
    except PolicyError as e:
        sys.stderr.write(f"policy_check rejected: {e}\\n")
        sys.exit(2)   # distinct exit code from invalid JSON

Or in pre-validate mode (returns sanitized payload + list of warnings):

    payload, warnings = check(..., mode="sanitize")

Adapted from Nate B. Jones's OB1 integrations/agent-memory-api unsafe-write
filter. No external dependencies.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Tuple, List


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Sources that are EXPECTED to carry raw transcripts. Skip transcript checks.
TRANSCRIPT_SOURCES = {"pendant", "fathom", "vapi", "voice_agent", "claude_session"}

# Hard caps. The brain is for facts, not blobs.
MAX_PAYLOAD_BYTES = 64 * 1024        # 64 KB per event
MAX_SUMMARY_CHARS = 500              # summary should be one-paragraph max
MAX_CODE_LINES = 50                  # >50 lines of code = put it in a file, reference by URI

# Secret patterns. Order matters; check broader patterns last.
SECRET_PATTERNS = [
    # AWS
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                              "AWS access key"),
    # GitHub PAT (classic + fine-grained)
    (re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),                           "GitHub PAT (classic)"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}\b"),                   "GitHub PAT (fine-grained)"),
    # OpenAI / Anthropic
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),                            "OpenAI-style API key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),                      "Anthropic API key"),
    # Stripe
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),       "Stripe key"),
    # Twilio
    (re.compile(r"\bSK[0-9a-fA-F]{32}\b"),                              "Twilio API key SID"),
    # Slack
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),                   "Slack token"),
    # Linear, ClickUp, Vapi
    (re.compile(r"\blin_api_[A-Za-z0-9]{20,}\b"),                       "Linear API key"),
    (re.compile(r"\bpk_[A-Za-z0-9]{20,}\b"),                            "ClickUp/Vapi-style API key"),
    # Generic: JSON value that looks like a long secret on a "..key/token/secret.." field
    (re.compile(r'"(?:[a-z_-]*(?:secret|token|api[_-]?key|password|passphrase|private[_-]?key)[a-z_-]*)"\s*:\s*"([^"]{20,})"', re.IGNORECASE),
                                                                         "secret-looking key in JSON"),
]


class PolicyError(Exception):
    """Raised by check() in strict mode when a payload violates policy."""


@dataclass
class PolicyResult:
    payload_json: str
    warnings: List[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(
    source: str,
    etype: str,
    payload_raw: str,
    mode: str = "strict",
) -> str | Tuple[str, List[str]]:
    """
    Validate (and optionally sanitize) a payload.

    Args:
        source: the event source (e.g. 'pendant', 'clickup', 'agent:scout').
        etype:  the event type (e.g. 'session_summary', 'task').
        payload_raw: JSON string of the payload.
        mode: 'strict' (raise PolicyError) or 'sanitize' (redact + return warnings).

    Returns:
        - strict mode: the validated payload JSON string (unchanged if no violations).
        - sanitize mode: (sanitized_payload_json, list_of_warning_strings).

    Raises:
        PolicyError (strict mode only) on any violation.
    """
    if mode not in ("strict", "sanitize"):
        raise ValueError("mode must be 'strict' or 'sanitize'")

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as e:
        raise PolicyError(f"payload is not valid JSON: {e}")

    if not isinstance(payload, dict):
        raise PolicyError("payload must be a JSON object at the top level")

    warnings: List[str] = []

    # 1. Size cap
    raw_bytes = len(payload_raw.encode("utf-8"))
    if raw_bytes > MAX_PAYLOAD_BYTES:
        msg = (
            f"payload is {raw_bytes} bytes (limit {MAX_PAYLOAD_BYTES}). "
            f"Move the body to a file and reference it via 'attachment_uri'."
        )
        if mode == "strict":
            raise PolicyError(msg)
        warnings.append(msg)

    # 2. summary presence and length
    summary = payload.get("summary")
    if not summary or not isinstance(summary, str):
        msg = "payload.summary is required (a short human-readable string)"
        if mode == "strict":
            raise PolicyError(msg)
        warnings.append(msg)
    elif len(summary) > MAX_SUMMARY_CHARS:
        msg = (
            f"payload.summary is {len(summary)} chars (limit {MAX_SUMMARY_CHARS}). "
            f"Move the long form into another field; summary should be a hook."
        )
        if mode == "strict":
            raise PolicyError(msg)
        warnings.append(msg)

    # 3. Secrets sweep across all string values
    for field_path, value in _walk_strings(payload):
        for pattern, label in SECRET_PATTERNS:
            m = pattern.search(value)
            if m:
                msg = f"found {label} at payload.{field_path}"
                if mode == "strict":
                    raise PolicyError(msg)
                # sanitize: redact the match
                redacted = pattern.sub(f"<REDACTED:{label}>", value)
                _set_path(payload, field_path, redacted)
                warnings.append(msg + " (redacted)")

    # 4. Code-blob check (only if not a transcript source)
    if source not in TRANSCRIPT_SOURCES:
        for field_path, value in _walk_strings(payload):
            line_count = value.count("\n") + 1 if value else 0
            if line_count > MAX_CODE_LINES and _looks_like_code(value):
                msg = (
                    f"payload.{field_path} is {line_count} lines of code-like text "
                    f"(limit {MAX_CODE_LINES}). Store the file separately and use 'attachment_uri'."
                )
                if mode == "strict":
                    raise PolicyError(msg)
                warnings.append(msg)

    # 5. Raw transcript dump check
    if source not in TRANSCRIPT_SOURCES and etype != "transcript":
        if "transcript" in payload and isinstance(payload["transcript"], str):
            n = len(payload["transcript"])
            if n > 4000:
                msg = (
                    f"payload.transcript is {n} chars but source={source} is not a "
                    f"transcript source. Use source='pendant' / 'fathom' / etc., or "
                    f"move the transcript to an attachment."
                )
                if mode == "strict":
                    raise PolicyError(msg)
                warnings.append(msg)

    sanitized_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    if mode == "strict":
        return sanitized_json
    return sanitized_json, warnings


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _walk_strings(obj, prefix=""):
    """Yield (dotted-path, string-value) for every string leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{prefix}[{i}]")
    elif isinstance(obj, str):
        yield prefix, obj


def _set_path(obj, dotted_path, value):
    """Set a string value at a dotted path inside a nested dict/list."""
    parts = re.split(r"\.|(\[\d+\])", dotted_path)
    parts = [p for p in parts if p]
    cur = obj
    for p in parts[:-1]:
        if p.startswith("[") and p.endswith("]"):
            cur = cur[int(p[1:-1])]
        else:
            cur = cur[p]
    last = parts[-1]
    if last.startswith("[") and last.endswith("]"):
        cur[int(last[1:-1])] = value
    else:
        cur[last] = value


# Heuristic: looks like code if it has many braces/semicolons/indent
def _looks_like_code(s: str) -> bool:
    if len(s) < 200:
        return False
    code_signals = (
        s.count("\n    ") + s.count("\n\t") +
        s.count("{") + s.count("}") +
        s.count(";") + s.count("=>") +
        s.count("def ") + s.count("function ") + s.count("import ")
    )
    return code_signals > 20


if __name__ == "__main__":
    # Self-test
    import sys
    sample = sys.stdin.read() if not sys.stdin.isatty() else (
        '{"summary": "test event", "note": "hello world", "key": "AKIAIOSFODNN7EXAMPLE"}'
    )
    try:
        out = check(source="test", etype="note", payload_raw=sample, mode="strict")
        print("OK:", out)
    except PolicyError as e:
        print("REJECTED:", e, file=sys.stderr)
        sys.exit(2)
