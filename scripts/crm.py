#!/usr/bin/env python3
"""Portable CRM tools over the existing OpenBrane event log.

Adapted from the live Brain outreach history and contact-state algorithms.
Every write uses record_event.append_event; contacts/deals are derived views.
Touches are reported observations, never sends or authenticated delivery proof.
The configured business binds this owner-controlled installation's CRM tools.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crm_providers import CRMProviderError, load_provider, validate_facts
from record_event import append_event

SOURCE = "crm"
KINDS = {"lead", "customer", "contact", "partner", "internal", "test", "automated", "dead"}
POLICIES = {"allowed", "hold", "do_not_contact", "unknown"}
BLOCK_KINDS = {"customer", "internal", "test", "automated", "dead"}
CHANNELS = {"email", "sms", "call", "phone", "linkedin", "social", "meeting", "other"}


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must explicitly use UTC")
    return parsed


def _timestamp(value, *, now=None, future=False):
    parsed = _parse(value)
    if not future and parsed > _now(now):
        raise ValueError("observation timestamp cannot be in the future")
    return _iso(parsed)


def _positive_env(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        raise ValueError(name + " must be a positive number") from None
    if not 0 < value < float("inf"):
        raise ValueError(name + " must be a positive finite number")
    return value


def _id(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(label + " must be a nonempty string of at most 200 characters")
    return value.strip()


def _business(business_id=None):
    configured = _id(os.environ.get("CRM_BUSINESS_ID", "example-business"), "CRM_BUSINESS_ID")
    if business_id is not None and business_id != configured:
        raise ValueError("business_id does not match the configured CRM business")
    return configured


def _email(value):
    if not isinstance(value, str):
        raise ValueError("email must be a string")
    normalized = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise ValueError("email must be a complete email address")
    return normalized


def _phone(value):
    if not isinstance(value, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
        raise ValueError("phone must use explicit E.164 format including country code")
    return value


def _identity(value):
    value = _id(value, "contact")
    if "@" in value:
        return _email(value)
    if value.startswith("phone:"):
        return "phone:" + _phone(value[6:])
    if value.startswith("+"):
        return "phone:" + _phone(value)
    return value


def _path(db_path=None):
    return Path(db_path or os.environ.get("BRAIN_DB", Path(__file__).resolve().parent.parent / "events.db"))


@contextmanager
def _connection(db_path=None, *, write=False):
    path = _path(db_path)
    if not path.is_file():
        raise ValueError("Brain database is missing; initialize OpenBrane first")
    conn = sqlite3.connect(str(path) if write else path.resolve().as_uri() + "?mode=ro",
                           uri=not write, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


def _events(conn, business, event_type=None):
    sql = "SELECT id,ts,type,payload_json FROM events WHERE source=? AND json_extract(payload_json,'$.business_id')=?"
    args = [SOURCE, business]
    if event_type is not None:
        sql += " AND type=?"
        args.append(event_type)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [{"event_id": r["id"], "ts": r["ts"], "type": r["type"],
             "payload": json.loads(r["payload_json"])} for r in rows]


def _contacts(conn, business):
    current = {}
    for event in _events(conn, business, "crm.contact"):
        record = dict(event["payload"])
        record["event_id"] = event["event_id"]
        current[record["contact_id"]] = record
    for record in current.values():
        record.update(last_inbound_ts=None, last_outbound_ts=None)
    for event in _events(conn, business, "crm.touch"):
        touch = event["payload"]
        contact = current.get(touch["contact_id"])
        if contact is None:
            continue
        field = "last_inbound_ts" if touch["direction"] == "inbound" else "last_outbound_ts"
        if contact[field] is None or _parse(event["ts"]) > _parse(contact[field]):
            contact[field] = event["ts"]
    for contact in current.values():
        incoming, outgoing = contact["last_inbound_ts"], contact["last_outbound_ts"]
        turn = "awaiting_us" if incoming and (not outgoing or _parse(incoming) > _parse(outgoing)) else "awaiting_them" if outgoing else "none"
        contact["whose_turn"] = turn
        latest = max((_parse(x) for x in (incoming, outgoing) if x), default=None)
        contact["status"] = "dead" if contact["kind"] == "dead" else "dormant" if latest and (_now() - latest).total_seconds() > _positive_env("CRM_DORMANT_DAYS", 21) * 86400 else turn
    return current


def _resolve(query, contacts):
    key = _identity(query)
    exact = [c for c in contacts.values() if key in {c["contact_id"], c.get("email"), "phone:" + c["phone"] if c.get("phone") else None}]
    if exact or "@" in key or key.startswith("phone:"):
        return exact
    return [c for c in contacts.values() if key.lower() in c.get("display_name", "").lower()]


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    return value


def upsert_contact(contact_id, email=None, phone=None, display_name=None, kind="contact",
                   contact_policy="unknown", business_id=None, observed_at=None, valid_until=None,
                   *, db_path=None, now=None):
    business = _business(business_id)
    contact_id = _id(contact_id, "contact_id")
    if kind not in KINDS or contact_policy not in POLICIES:
        raise ValueError("unsupported contact kind or contact_policy")
    stamp = _timestamp(observed_at, now=now) if observed_at else _iso(_now(now))
    expiry = _timestamp(valid_until, now=now, future=True) if valid_until else _iso(_parse(stamp) + timedelta(hours=_positive_env("CRM_CONTACT_TTL_HOURS", 2)))
    if _parse(expiry) < _parse(stamp):
        raise ValueError("valid_until cannot precede observed_at")
    record = {"business_id": business, "contact_id": contact_id,
              "display_name": _id(display_name or contact_id, "display_name"), "kind": kind,
              "contact_policy": contact_policy, "observed_at": stamp, "valid_until": expiry,
              "summary": "CRM contact: " + (display_name or contact_id)}
    if email is not None:
        record["email"] = _email(email)
    if phone is not None:
        record["phone"] = _phone(phone)
    with _connection(db_path, write=True) as conn:
        existing = _contacts(conn, business)
        for item in existing.values():
            if item["contact_id"] != contact_id and any(record.get(f) is not None and item.get(f) == record[f] for f in ("email", "phone")):
                raise ValueError("contact identity already belongs to another contact in this business")
        result = append_event(source=SOURCE, event_type="crm.contact", payload=record,
                              actor=contact_id, ts=stamp, connection=conn)
    return {**record, "event_id": result["event_id"]}


def find_contact(query, limit=10, business_id=None, *, db_path=None):
    business = _business(business_id)
    with _connection(db_path) as conn:
        return _resolve(query, _contacts(conn, business))[:_limit(limit)]


def upsert_deal(deal_id, contact_id, title, stage="open", owner=None, next_action=None,
                value_cents=None, currency=None, business_id=None, *, db_path=None):
    business = _business(business_id)
    record = {"business_id": business, "deal_id": _id(deal_id, "deal_id"),
              "contact_id": _id(contact_id, "contact_id"), "title": _id(title, "title"),
              "stage": _id(stage, "stage"), "owner": owner, "next_action": next_action,
              "summary": "CRM opportunity: " + title}
    if (value_cents is None) != (currency is None):
        raise ValueError("value_cents and currency must be supplied together")
    if value_cents is not None:
        if isinstance(value_cents, bool) or not isinstance(value_cents, int) or value_cents < 0:
            raise ValueError("value_cents must be a nonnegative minor-unit integer")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be a three-letter uppercase currency code")
        record.update(value_cents=value_cents, currency=currency)
    with _connection(db_path, write=True) as conn:
        if contact_id not in _contacts(conn, business):
            raise ValueError("deal contact does not exist in the configured business")
        result = append_event(source=SOURCE, event_type="crm.deal", payload=record,
                              actor=deal_id, connection=conn)
    return {**record, "event_id": result["event_id"], "revenue_verified": False}


def get_pipeline(limit=50, business_id=None, *, db_path=None):
    business = _business(business_id)
    with _connection(db_path) as conn:
        current = {}
        for event in _events(conn, business, "crm.deal"):
            record = {**event["payload"], "event_id": event["event_id"], "revenue_verified": False}
            current[record["deal_id"]] = record
    return list(current.values())[:_limit(limit)]


def log_outreach(contact, mailbox, agent, channel="email", subject="", message_id=None,
                 thread_id=None, ts=None, direction="outbound", business_id=None, *, db_path=None):
    """Record a reported interaction; no send, provider execution or delivery claim."""
    business = _business(business_id)
    if channel not in CHANNELS or direction not in {"inbound", "outbound"}:
        raise ValueError("unsupported channel or direction")
    if message_id is None:
        raise ValueError("message_id is required for deterministic retry identity")
    message_id = _id(message_id, "message_id")
    timestamp = _timestamp(ts) if ts is not None else None
    with _connection(db_path, write=True) as conn:
        matches = _resolve(contact, _contacts(conn, business))
        if len(matches) != 1:
            raise ValueError("touch requires one existing contact in the configured business")
        resolved = matches[0]
        key = resolved.get("email") or ("phone:" + resolved["phone"] if resolved.get("phone") else resolved["contact_id"])
        source_ref = channel + ":" + message_id + "#" + key
        record = {"business_id": business, "contact_id": resolved["contact_id"], "contact": key,
                  "mailbox": _id(mailbox, "mailbox"), "agent": _id(agent, "agent"),
                  "channel": channel, "direction": direction, "subject": str(subject or "")[:300],
                  "message_id": message_id, "thread_id": thread_id, "source_ref": source_ref,
                  "observation_status": "reported", "summary": "Reported " + direction + " " + channel + " interaction"}
        dedupe = json.dumps([business, direction, channel, message_id, resolved["contact_id"]], separators=(",", ":"))
        result = append_event(source=SOURCE, event_type="crm.touch", payload=record,
                              actor=resolved["contact_id"], ts=timestamp, dedupe_key=dedupe, connection=conn)
    return {**record, "event_id": result["event_id"], "ts": result["ts"],
            "deduped": result["deduped"], "sent": False, "delivery_verified": False}


def outreach_history(contact=None, mailbox=None, agent=None, since=None, limit=50,
                     business_id=None, *, db_path=None):
    business = _business(business_id)
    since_dt = _parse(since) if since else None
    with _connection(db_path) as conn:
        contacts = _contacts(conn, business)
        ids = {c["contact_id"] for c in _resolve(contact, contacts)} if contact else None
        records = []
        for event in _events(conn, business, "crm.touch"):
            row = {**event["payload"], "event_id": event["event_id"], "ts": event["ts"], "delivery_verified": False}
            if ids is not None and row["contact_id"] not in ids:
                continue
            if mailbox and row["mailbox"] != mailbox:
                continue
            if agent and row["agent"] != agent:
                continue
            if since_dt and _parse(row["ts"]) < since_dt:
                continue
            records.append(row)
    records.sort(key=lambda r: (_parse(r["ts"]), r["event_id"]), reverse=True)
    return records[:_limit(limit)]


def _provider_facts(contact, business):
    provider = None
    try:
        provider = load_provider()
        if provider is None:
            return None
        email, phone = contact.get("email"), contact.get("phone")
        if not email and not phone:
            raise CRMProviderError("contact has no identity for external provider lookup")
        facts = validate_facts(provider.contact_facts(business, email=email, phone=phone if not email else None), business)
        if facts is None:
            raise CRMProviderError("configured external CRM provider has no contact facts")
        if facts is not None and ((email and facts.get("email") != email) or (not email and facts.get("phone") != phone)):
            raise CRMProviderError("provider contact identity did not match the requested contact")
        return facts
    except CRMProviderError:
        raise
    except Exception:
        raise CRMProviderError("CRM provider lookup failed") from None
    finally:
        if provider is not None and callable(getattr(provider, "close", None)):
            try:
                provider.close()
            except Exception:
                pass


def check_outreach(contact, cooldown_days=7, mailbox=None, business_id=None, *, db_path=None, now=None):
    """Combined policy facts, never permission to send. Fail closed on unknown state."""
    business = _business(business_id)
    if isinstance(cooldown_days, bool) or not isinstance(cooldown_days, int) or cooldown_days < 0:
        raise ValueError("cooldown_days must be a nonnegative integer")
    current_time = _now(now)
    result = {"business_id": business, "contact": contact, "send_authorized": False}
    def decision(verdict, reason, exit_code=1):
        return {**result, "verdict": verdict, "reason": reason, "exit": exit_code}
    try:
        matches = find_contact(contact, business_id=business, db_path=db_path)
        if len(matches) != 1:
            return decision("caution", "contact is unknown or ambiguous")
        row = matches[0]
        result.update(contact_id=row["contact_id"], kind=row["kind"], status=row["status"], whose_turn=row["whose_turn"])
        hist = outreach_history(row["contact_id"], business_id=business, db_path=db_path)
        result.update(history=hist, history_count=len(hist))
        if row["kind"] in BLOCK_KINDS or row["contact_policy"] == "do_not_contact":
            return decision("do_not_contact", "contact is excluded from outreach", 2)
        if row["contact_policy"] in {"hold", "unknown"}:
            return decision("caution", "contact policy is held or unknown")
        if _parse(row["valid_until"]) <= current_time or _parse(row["observed_at"]) > current_time:
            return decision("caution", "contact policy snapshot is stale or future-dated")
        facts = _provider_facts(row, business)
        last_in, last_out = row["last_inbound_ts"], row["last_outbound_ts"]
        if facts:
            if facts.get("lifecycle_status") in BLOCK_KINDS | {"do_not_contact"}:
                return decision("do_not_contact", "external CRM contact is excluded from outreach", 2)
            if facts.get("lifecycle_status") not in {"lead", "prospect", "contact", "partner", "churned"}:
                return decision("caution", "external CRM contact policy is held or unknown")
            updated = facts.get("updated_at")
            if not updated or current_time - _parse(updated) > timedelta(hours=_positive_env("CRM_CONTACT_TTL_HOURS", 2)) or _parse(updated) > current_time:
                return decision("caution", "external CRM contact snapshot is stale or unknown")
            incoming = [x for x in (last_in, facts.get("last_inbound_at")) if x]
            outgoing = [x for x in (last_out, facts.get("last_outbound_at")) if x]
            last_in = max(incoming, key=_parse, default=None)
            last_out = max(outgoing, key=_parse, default=None)
        if any(_parse(x) > current_time for x in (last_in, last_out) if x):
            return decision("caution", "contact history contains a future observation")
        result.update(last_inbound_ts=last_in, last_outbound_ts=last_out)
        if last_in and (not last_out or _parse(last_in) > _parse(last_out)):
            result["whose_turn"] = "awaiting_us"
            return decision("clear_to_contact", "their inbound is newer than our last outbound", 0)
        if last_out:
            age = (current_time - _parse(last_out)).total_seconds() / 86400
            result["whose_turn"] = "awaiting_them"
            if age < cooldown_days:
                return decision("caution", "recently_contacted: outbound is inside cooldown")
            if age < _positive_env("CRM_DORMANT_DAYS", 21):
                return decision("caution", "awaiting_reply: they owe the next reply")
        return decision("clear_to_contact", "fresh contact policy allows contact; external send authority still required", 0)
    except (ValueError, sqlite3.Error, CRMProviderError):
        return decision("caution", "contact facts are unavailable or invalid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check_outreach", "outreach_history", "find_contact", "log_outreach", "upsert_contact", "upsert_deal", "get_pipeline"])
    parser.add_argument("--arguments", help="JSON object; omitted reads stdin")
    args = parser.parse_args()
    import sys
    try:
        arguments = json.loads(args.arguments if args.arguments is not None else sys.stdin.read())
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        result = globals()[args.operation](**arguments)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError, sqlite3.Error, CRMProviderError):
        print(json.dumps({"status": "error", "error": "CRM operation rejected; check configuration, scope and inputs"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
