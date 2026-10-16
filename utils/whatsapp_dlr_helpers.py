"""
utils/whatsapp_dlr_helpers.py

DLR (delivery-receipt) verification for the WhatsApp E2E campaign test
(tests/whatsapp/campaigns/test_whatsapp_campaign_create_flow.py::
test_e2e_whatsapp_campaign_creation_and_persistence), via
utils/whatsapp_campaign_dlr.py.

Retrieval mechanism (per the project owner's explicit instruction,
2026-10-11: "use same get api which we [use for] sms and rcs"):

    GET {dlr_base_url}/api/v1/dlr/{message_id}

The EXACT SAME endpoint/host/client already used by SMS (utils/
dlr_helpers.py) and RCS (utils/rcs_dlr_helpers.py) -- this module reuses
utils.sms_api_client.SmsApiClient.get_dlr() for the raw HTTP call rather
than building a third client. Only the RESPONSE SHAPE is new: for
WhatsApp it is a Meta Cloud API-style webhook payload, CONFIRMED by the
project owner pasting a real example (2026-10-11), and CONFIRMED to be
the SAME shape returned by the GET lookup (not a different shape the
way RCS's GET differs from its own inbound event):

  {"object": "whatsapp_business_account",
   "entry": [
     {"id": "2021963901925848",
      "changes": [
        {"value": {
           "messaging_product": "whatsapp",
           "metadata": {"display_phone_number": "919202511257",
                        "phone_number_id": "872397042626020"},
           "contacts": [{"wa_id": "919542783156",
                        "user_id": "IN.779847591872015"}],
           "statuses": [
             {"id": "wamid.HBgMOTE5NTQyNzgzMTU2FQIAERgSMzQ5OEJBRERDMjM5NTFGQjBFAA==",
              "status": "sent",
              "timestamp": "1791269419",
              "recipient_id": "919542783156",
              "recipient_user_id": "IN.779847591872015",
              "pricing": {"billable": True, "pricing_model": "PMP",
                          "category": "marketing_lite", "type": "regular"},
              "correlation_id": "test correlation_id",
              "message_id": "ac7f78b8-d3f0-4e8a-84c3-af68aec4f6b6"}]},
         "field": "messages"}],
      "correlation_id": "test correlation_id",
      "message_id": "wamid.HBgMOTE5NTQyNzgzMTU2FQIAERgSMzQ5OEJBRERDMjM5NTFGQjBFAA=="}]}

Two DISTINCT message identifiers appear in every real sample, confirmed
by also reading the live "Message Details" modal
(pages/whatsapp/campaigns/whatsapp_campaign_report_page.py's
MODAL_SECTION_BASIC_INFO fields, captured 2026-10-11 against a real
sent message):

  - INTERNAL message id: `statuses[0].message_id` -- a UUID
    (e.g. "ac7f78b8-...", confirmed identical in form to the modal's
    own "Message ID:" field, e.g. "0cf4eb15-5c04-4fe7-9b54-85c1b015c88b").
    THIS is what GET {dlr_base_url}/api/v1/dlr/{message_id} is queried
    with, and what this module correlates against.
  - EXTERNAL/provider message id: `statuses[0].id` == the top-level
    `entry[0].message_id` -- Meta's own "wamid...." identifier. Recorded
    for diagnostics only, never required to match anything captured
    before dispatch (no earlier capture of it exists in this flow).

`statuses[0].timestamp` is a UNIX epoch SECONDS string (e.g.
"1791269419"), NOT ISO-8601 like RCS's `timestamp` field -- confirmed by
the real sample above. is_valid_unix_epoch_seconds() below validates
this format specifically; do not reuse RCS's ISO-8601 validator here.

Per the task spec's explicit "do nots":
  - Never assume every WhatsApp provider payload/status uses the same
    structure -- the schema check below only requires the Meta Cloud
    API fields actually confirmed above; it does not invent a universal
    cross-provider envelope.
  - Never fail a generic DLR-arrival check merely because the final
    status isn't "delivered" -- SUCCESS_STATUSES below accepts any of
    Meta's real non-terminal-failure statuses (sent/delivered/read) as
    "a DLR was received for this message". `status == "failed"` is
    treated as an EXPLICIT REJECTION (a distinct, terminal outcome from
    "no DLR yet"), never as "arrived and passed".
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

# ── Configuration (env-overridable; polling must stay configurable, no
# fixed sleep, per spec) ──────────────────────────────────────────────────
WHATSAPP_DLR_POLL_TIMEOUT_SECONDS = int(os.getenv("WHATSAPP_DLR_POLL_TIMEOUT_SECONDS", "120"))
WHATSAPP_DLR_POLL_INTERVAL_SECONDS = int(os.getenv("WHATSAPP_DLR_POLL_INTERVAL_SECONDS", "5"))

STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"
STATUS_FAILED = "failed"

# Any of these means "a real DLR status update was received for this
# message" -- per spec, a generic arrival check must not require
# specifically "delivered" (a provider may only ever report "sent" for
# some message types/providers).
SUCCESS_STATUSES = {STATUS_SENT, STATUS_DELIVERED, STATUS_READ}
REJECTED_STATUSES = {STATUS_FAILED}


class WhatsAppDlrSchemaError(AssertionError):
    """Raised with a human-readable list of what's wrong with a webhook
    payload's structure; `missing_fields`/`invalid_fields` carry the
    machine-readable detail for a failure report."""

    def __init__(self, message, missing_fields=None, invalid_fields=None):
        super().__init__(message)
        self.missing_fields = missing_fields or []
        self.invalid_fields = invalid_fields or []


def _get_path(obj: Any, path: str):
    """Reads a dotted path out of nested dicts/lists -- a numeric segment
    indexes a list (e.g. "entry.0.changes.0.value.statuses.0.message_id").
    Returns (found, value); found=False if any segment is missing or a
    list index is out of range (never raises, never confuses "present but
    null/empty" with "missing")."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        elif isinstance(cur, list):
            if not part.lstrip("-").isdigit():
                return False, None
            idx = int(part)
            if idx < 0 or idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            return False, None
    return True, cur


def is_valid_unix_epoch_seconds(value: Any) -> bool:
    """True if `value` is a string/int of whole seconds since the epoch
    that parses to a sane (not absurdly far past/future) date -- the
    confirmed real format of statuses[0].timestamp ("1791269419"), NOT
    ISO-8601 (RCS's timestamp format is different; do not conflate the
    two)."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return False
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    return 2000 <= dt.year <= 2100


_DIGITS_RE = re.compile(r"\D+")


def normalize_phone(value: Any) -> str:
    """Strips everything but digits (and a leading '+') so a UI-rendered
    recipient like "918123456780 (IN)" can be compared against a
    webhook's plain-digits recipient_id -- never assumes identical
    formatting between the two sources."""
    if value is None:
        return ""
    s = str(value)
    plus = s.strip().startswith("+")
    digits = _DIGITS_RE.sub("", s)
    return ("+" + digits) if plus else digits


# ── Schema validation ─────────────────────────────────────────────────────
#
# Fields confirmed present in the real sample above. This is deliberately
# narrow (the Meta Cloud API "messages" status-webhook envelope actually
# observed) rather than a speculative cross-provider superset.

WHATSAPP_DLR_REQUIRED_FIELDS = [
    "object",
    "entry",
    "entry.0.id",
    "entry.0.changes",
    "entry.0.changes.0.field",
    "entry.0.changes.0.value.messaging_product",
    "entry.0.changes.0.value.metadata.display_phone_number",
    "entry.0.changes.0.value.metadata.phone_number_id",
    "entry.0.changes.0.value.statuses",
    "entry.0.changes.0.value.statuses.0.id",
    "entry.0.changes.0.value.statuses.0.status",
    "entry.0.changes.0.value.statuses.0.timestamp",
    "entry.0.changes.0.value.statuses.0.recipient_id",
    "entry.0.changes.0.value.statuses.0.message_id",
]

# Legitimately blank/absent in a real sample (e.g. no correlation_id was
# set at send time) -- presence of the KEY is not required for these;
# never fail the schema check for a missing/blank value here.
WHATSAPP_DLR_OPTIONAL_FIELDS = {
    "entry.0.changes.0.value.statuses.0.correlation_id",
    "entry.0.changes.0.value.statuses.0.recipient_user_id",
    "entry.0.changes.0.value.statuses.0.pricing",
    "entry.0.changes.0.value.contacts",
    "entry.0.correlation_id",
    "entry.0.message_id",
}

WHATSAPP_DLR_TIMESTAMP_FIELDS = ["entry.0.changes.0.value.statuses.0.timestamp"]


def validate_whatsapp_dlr_schema(body: Any) -> tuple[bool, list, list]:
    """Validates `body` against the confirmed Meta Cloud API status-
    webhook envelope. Returns (is_valid, missing_fields, invalid_timestamps).
    Never requires a field listed in WHATSAPP_DLR_OPTIONAL_FIELDS."""
    if not isinstance(body, dict):
        return False, ["<entire DLR response body is not a JSON object>"], []

    missing = []
    for path in WHATSAPP_DLR_REQUIRED_FIELDS:
        found, _ = _get_path(body, path)
        if not found:
            missing.append(path)

    invalid_timestamps = []
    for path in WHATSAPP_DLR_TIMESTAMP_FIELDS:
        found, value = _get_path(body, path)
        if not found:
            continue  # already reported above
        if not is_valid_unix_epoch_seconds(value):
            invalid_timestamps.append((path, value))

    return (not missing and not invalid_timestamps), missing, invalid_timestamps


def extract_status_fields(body: Any) -> dict:
    """Flattens the confirmed envelope's first status entry into one
    dict: internal_message_id (statuses[0].message_id, the UUID used
    for polling/correlation), external_message_id (statuses[0].id /
    entry-level message_id, the wamid), status, timestamp, recipient_id,
    correlation_id. Any field not found is None -- never fabricated."""
    fields = {
        "internal_message_id": None,
        "external_message_id": None,
        "status": None,
        "timestamp": None,
        "recipient_id": None,
        "correlation_id": None,
    }
    if not isinstance(body, dict):
        return fields

    _, fields["internal_message_id"] = _get_path(body, "entry.0.changes.0.value.statuses.0.message_id")
    found_ext, ext = _get_path(body, "entry.0.changes.0.value.statuses.0.id")
    if not found_ext or not ext:
        found_ext, ext = _get_path(body, "entry.0.message_id")
    fields["external_message_id"] = ext if found_ext else None
    _, fields["status"] = _get_path(body, "entry.0.changes.0.value.statuses.0.status")
    _, fields["timestamp"] = _get_path(body, "entry.0.changes.0.value.statuses.0.timestamp")
    _, fields["recipient_id"] = _get_path(body, "entry.0.changes.0.value.statuses.0.recipient_id")
    found_corr, corr = _get_path(body, "entry.0.changes.0.value.statuses.0.correlation_id")
    fields["correlation_id"] = corr if found_corr else None
    return fields


# ── Correlation ────────────────────────────────────────────────────────────

def correlate_whatsapp_event(body: dict, expected_message_id: str,
                              expected_recipient: Optional[str] = None) -> dict:
    """Checks that `body` genuinely belongs to the message the test
    sent: internal message id always; recipient only when the caller
    has one (phone-format-normalized on both sides, since the UI and
    the webhook render numbers differently -- see normalize_phone()).
    Returns a dict of individual pass/fail checks, never raises."""
    result = {"internal_message_id_match": None, "recipient_match": None}
    fields = extract_status_fields(body)

    result["internal_message_id_match"] = (
        expected_message_id is not None
        and fields["internal_message_id"] == expected_message_id
    )

    if expected_recipient is not None:
        actual = normalize_phone(fields["recipient_id"])
        expected = normalize_phone(expected_recipient)
        # Compare suffix-wise too -- the UI may show a number with a
        # leading '+'/country separator the webhook's recipient_id omits,
        # or vice versa; never invent a specific stripping rule beyond
        # digit normalization already applied by normalize_phone().
        result["recipient_match"] = bool(actual) and bool(expected) and (
            actual == expected or actual.endswith(expected) or expected.endswith(actual)
        )

    return result


# ── Polling ──────────────────────────────────────────────────────────────

def poll_for_whatsapp_dlr(api_client, message_id: str,
                           timeout_seconds: int = WHATSAPP_DLR_POLL_TIMEOUT_SECONDS,
                           interval_seconds: int = WHATSAPP_DLR_POLL_INTERVAL_SECONDS):
    """Polls GET {dlr_base_url}/api/v1/dlr/{message_id} (via
    SmsApiClient.get_dlr(), the same endpoint/client SMS and RCS DLR
    tests already use) until EITHER:
      - a status in SUCCESS_STATUSES is observed for THIS message_id
        (an accepted/sent/delivered/read DLR genuinely arrived), or
      - a status in REJECTED_STATUSES is observed (explicitly rejected
        -- stops polling immediately rather than waiting out the full
        timeout for a message that will never progress further), or
      - the timeout elapses with no DLR for this message_id at all.

    Returns (last_response, last_body, events_seen, outcome) where
    outcome is one of "received", "rejected", "timeout", or
    "unexpected_response" (a non-200 / non-JSON response -- distinct
    from "no DLR yet", per spec's "receiver returned unexpected
    response" failure category). events_seen is the list of every
    DISTINCT status observed for this message_id during polling.
    Never raises -- callers build the exact failure report from the
    returned outcome instead of catching an exception."""
    last_response = None
    last_body = None
    events_seen = []
    seen = set()

    deadline = time.time() + timeout_seconds
    while True:
        last_response = api_client.get_dlr(message_id)
        if last_response is not None and last_response.status_code == 200:
            try:
                last_body = last_response.json()
            except Exception:
                last_body = None
            if isinstance(last_body, dict):
                fields = extract_status_fields(last_body)
                if fields["internal_message_id"] == message_id and fields["status"]:
                    if fields["status"] not in seen:
                        seen.add(fields["status"])
                        events_seen.append(fields["status"])
                    if fields["status"] in REJECTED_STATUSES:
                        return last_response, last_body, events_seen, "rejected"
                    if fields["status"] in SUCCESS_STATUSES:
                        return last_response, last_body, events_seen, "received"
        elif last_response is not None and last_response.status_code not in (200, 404):
            # A real error (5xx, auth failure, etc.) -- distinct from
            # "not found yet" (404, treated as "no DLR yet" below).
            return last_response, last_body, events_seen, "unexpected_response"
        if time.time() >= deadline:
            return last_response, last_body, events_seen, "timeout"
        time.sleep(interval_seconds)


# ── Failure report (spec section 7/8 categories, exact field order) ──────

FAILURE_TIMEOUT = "DLR not received before timeout"
FAILURE_MESSAGE_ID_MISMATCH = "message ID mismatch"
FAILURE_RECIPIENT_MISMATCH = "recipient mismatch"
FAILURE_INVALID_TIMESTAMP = "invalid or missing timestamp"
FAILURE_SCHEMA = "payload doesn't match contract"
FAILURE_UNEXPECTED_RESPONSE = "receiver returned unexpected response"
FAILURE_NO_MESSAGE_ID_CAPTURED = "message ID could not be captured from the UI"


def build_failure_report(*, campaign_name, internal_message_id, external_message_id,
                          recipient, actual_status, dlr_received,
                          schema_validation, correlation_validation,
                          failure_categories, failure_reason) -> str:
    def _yn(b):
        if b is None:
            return "N/A"
        return "YES" if b else "NO"

    def _pf(b):
        if b is None:
            return "N/A"
        return "PASS" if b else "FAIL"

    lines = [
        f"Campaign Name: {campaign_name}",
        f"Internal Message ID: {internal_message_id}",
        f"External Message ID (wamid): {external_message_id}",
        f"Recipient: {recipient}",
        f"Actual Status: {actual_status}",
        f"DLR Received: {_yn(dlr_received)}",
        f"Schema Validation: {_pf(schema_validation)}",
        f"Correlation Validation: {_pf(correlation_validation)}",
        f"Failure Categories: {', '.join(failure_categories) if failure_categories else 'none'}",
        f"Failure Reason: {failure_reason}",
    ]
    return "\n".join(lines)
