"""
utils/whatsapp_campaign_dlr.py

DLR (delivery-receipt) verification for a launched WhatsApp UI campaign,
wired directly into
tests/whatsapp/campaigns/test_whatsapp_campaign_create_flow.py::
test_e2e_whatsapp_campaign_creation_and_persistence -- added AFTER that
test's existing campaign-launch + list-persistence assertions. This adds
NO new test case/file and does NOT re-launch/re-submit the campaign.

Flow (per the task spec, 2026-10-11, and the project owner's follow-up
answers confirming the real receiver/UI contract):
  1. Campaign list -> search the campaign -> open its Reports page
     (WhatsAppCampaignPage.click_reports_link_on_first_row()).
  2. Wait until the report lists the campaign's recipients (a
     just-launched campaign fills its report asynchronously).
  3. UNLIKE RCS (whose report table exposes a `rowpk` attribute
     directly), WhatsApp's report table has NO structural message-id
     attribute on its rows (confirmed via a real DOM capture of this
     exact page, 2026-10-11) -- the internal message id only appears
     inside the per-row "Message Details" modal's "Basic Information"
     section (confirmed field "Message ID:", a UUID). So this module
     walks every row's View popup -- the SAME "popup-walking" pattern
     utils/campaign_dlr.py already uses for SMS, generalized here via
     WhatsAppCampaignReportPage.get_view_row_indices()/
     click_view_on_row() (added alongside this module; a direct,
     confirmed-safe generalization of the page's existing
     click_view_on_first_row(), reusing the same VIEW_ICON_IN_ROW
     locator for every row instead of only the first).
  4. For EACH collected (recipient, internal_message_id) pair, poll
     GET {dlr_base_url}/api/v1/dlr/{message_id} (via
     utils.whatsapp_dlr_helpers.poll_for_whatsapp_dlr(), reusing
     utils.sms_api_client.SmsApiClient.get_dlr() -- the SAME endpoint/
     client SMS and RCS DLR tests already use, per the project owner's
     explicit instruction: "use same get api which we [use for] sms and
     rcs") until a real status update is observed, explicitly rejected,
     or the timeout elapses.
  5. Validate the response's schema (validate_whatsapp_dlr_schema()),
     correlate it against the message id/recipient captured from the UI
     (correlate_whatsapp_event()).
  6. UNLIKE RCS's fail-fast loop, this module walks EVERY recipient
     first and tracks expected/received/missing/mismatched/rejected
     SEPARATELY (per spec section 6's explicit requirement), then raises
     ONE WhatsAppCampaignDlrError carrying a per-recipient failure report
     for every recipient that did not pass, plus the end-of-run summary
     (checked/received/missing/mismatched/rejected counts) -- mirroring
     utils/campaign_dlr.py's SMS aggregate-then-report style rather than
     RCS's stop-on-first-failure style, since the spec here explicitly
     asks for every category to be tracked and reported, not just the
     first failure encountered.

Deliberately NOT done, per the spec's explicit "do nots":
  - No DLR is ever generated/forced -- only observed via polling.
  - A successful campaign submission/UI "Delivered" status is never
    treated as DLR received on its own -- the webhook/GET response is
    the sole source of truth for "received".
  - A message whose id could not be captured from the UI is never
    silently dropped -- it is recorded as its own named failure
    category (FAILURE_NO_MESSAGE_ID_CAPTURED) in the final report.
  - No hardcoded message ids/recipients/campaign ids/statuses anywhere
    below -- every value is read from the UI or the DLR response.

Env (mirrors RCS_CAMPAIGN_*/SMS_CAMPAIGN_* naming):
  WHATSAPP_CAMPAIGN_DLR_VERIFY     "false" disables the check entirely (default: enabled)
  WHATSAPP_CAMPAIGN_DLR_TIMEOUT    seconds to wait for EACH message's DLR (default 120)
  WHATSAPP_CAMPAIGN_REPORT_TIMEOUT seconds to wait for the report rows to appear/stabilize (default 120)
  WHATSAPP_DLR_POLL_INTERVAL_SECONDS  poll interval for each GET (see utils/whatsapp_dlr_helpers.py; default 5)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from pages.whatsapp.campaigns.whatsapp_campaign_page import WhatsAppCampaignPage
from pages.whatsapp.campaigns.whatsapp_campaign_report_page import WhatsAppCampaignReportPage
from utils.whatsapp_dlr_helpers import (
    FAILURE_INVALID_TIMESTAMP,
    FAILURE_MESSAGE_ID_MISMATCH,
    FAILURE_NO_MESSAGE_ID_CAPTURED,
    FAILURE_RECIPIENT_MISMATCH,
    FAILURE_SCHEMA,
    FAILURE_TIMEOUT,
    FAILURE_UNEXPECTED_RESPONSE,
    WHATSAPP_DLR_POLL_INTERVAL_SECONDS,
    build_failure_report,
    correlate_whatsapp_event,
    extract_status_fields,
    poll_for_whatsapp_dlr,
    validate_whatsapp_dlr_schema,
)

DLR_VERIFY_ENABLED = os.getenv("WHATSAPP_CAMPAIGN_DLR_VERIFY", "true").strip().lower() not in ("0", "false", "no", "off")
DLR_TIMEOUT_SECONDS = int(os.getenv("WHATSAPP_CAMPAIGN_DLR_TIMEOUT", "120"))
REPORT_TIMEOUT_SECONDS = int(os.getenv("WHATSAPP_CAMPAIGN_REPORT_TIMEOUT", "120"))


class WhatsAppCampaignDlrError(AssertionError):
    """Raised with the full per-recipient failure report(s) plus the
    end-of-run summary as the message; `.failure_reports` carries the
    list of individual per-recipient report strings, `.summary` the
    [(label, value)] summary rows, for a caller that wants either
    without re-parsing the exception message."""

    def __init__(self, message, failure_reports=None, summary=None):
        super().__init__(message)
        self.failure_reports = failure_reports or []
        self.summary = summary or []


@dataclass
class WhatsAppCampaignDlrResult:
    campaign_name: str
    expected_count: int = 0
    received: dict = field(default_factory=dict)     # internal_message_id -> last DLR body
    missing: list = field(default_factory=list)       # [(recipient, internal_message_id)]
    mismatched: list = field(default_factory=list)    # [(recipient, internal_message_id, reasons)]
    rejected: list = field(default_factory=list)      # [(recipient, internal_message_id, last_body)]
    no_message_id: list = field(default_factory=list)  # [recipient/row label]
    summary: list = field(default_factory=list)

    def summary_text(self):
        width = max(len(k) for k, _ in self.summary) if self.summary else 0
        return "\n".join(f"{k.ljust(width)}   {v}" for k, v in self.summary)


def _record(record_property, key, value):
    if record_property is not None:
        record_property(key, value)


def _open_campaign_report(page, campaign_name, timeout_s):
    """Campaigns list -> search -> Reports of the (first/only) matching
    row. Retries until timeout, because a just-launched campaign may take
    a moment to be listed -- same rationale as the SMS/RCS reference
    implementations."""
    campaign_page = WhatsAppCampaignPage(page)
    deadline = time.time() + timeout_s
    found = False
    while True:
        campaign_page.navigate()
        campaign_page.wait_for_table_load()
        campaign_page.search(campaign_name)
        found = campaign_page.get_row_count() >= 1
        if found and campaign_page.click_reports_link_on_first_row():
            return "opened"
        if time.time() >= deadline:
            return "report" if found else "not_found"
        time.sleep(5)


def _wait_for_report_rows(report_page, timeout_s, expected_recipients):
    """Reload the report until it has rows and the count is stable (and,
    if known, at least `expected_recipients`). Returns the final row
    count."""
    deadline = time.time() + timeout_s
    previous = -1
    while True:
        try:
            report_page.wait_for_table_load()
            rows = report_page.get_row_count()
        except Exception:
            rows = 0
        enough = expected_recipients is None or rows >= expected_recipients
        if rows > 0 and rows == previous and enough:
            return rows
        if time.time() >= deadline:
            return rows
        previous = rows
        time.sleep(5)
        report_page.page.reload()


def _collect_recipients_and_message_ids(report_page):
    """Walks every row's View popup (see module docstring point 3) to
    read Contact Number + "Message ID" (Basic Information section) +
    Current Status. Returns (entries, no_message_id) where `entries` is
    a list of dicts {recipient, message_id, ui_status, row_label} and
    `no_message_id` names any row whose popup opened but had no
    extractable Message ID -- NEVER silently dropped, reported as its
    own failure category by the caller."""
    entries = []
    no_message_id = []
    indices = report_page.get_view_row_indices()
    for i in indices:
        row_label = f"row {i + 1}"
        if not report_page.click_view_on_row(i):
            no_message_id.append(f"{row_label} (View did not open)")
            continue
        if not report_page.is_message_view_popup_open():
            no_message_id.append(f"{row_label} (Message Details popup did not open)")
            continue
        try:
            recipient = report_page.get_modal_contact_number()
        except Exception:
            recipient = None
        try:
            ui_status = report_page.get_modal_current_status()
        except Exception:
            ui_status = None
        try:
            message_id = (report_page.get_modal_basic_info_field("Message ID") or "").strip()
        except Exception:
            message_id = ""
        report_page.close_message_view_popup()
        label = f"{recipient or row_label} ({row_label})"
        if not message_id:
            no_message_id.append(label)
            continue
        entries.append({
            "recipient": recipient,
            "message_id": message_id,
            "ui_status": ui_status,
            "row_label": row_label,
        })
    return entries, no_message_id


def verify_whatsapp_campaign_dlrs(page, api_client, campaign_name, record_property=None,
                                   expected_recipients=None, report_timeout=None,
                                   dlr_timeout=None, dlr_interval=None):
    """Runs the full WhatsApp campaign DLR check on Playwright `page`, for
    a campaign that has ALREADY been created and launched (Send Now) by
    the caller -- this function never submits/launches anything itself.

    Returns WhatsAppCampaignDlrResult on success (every expected
    recipient's DLR received and correlated); raises
    WhatsAppCampaignDlrError (an AssertionError, so the calling test
    FAILS) carrying a per-recipient failure report for every recipient
    that did not pass, plus the end-of-run summary, if anything failed.
    """
    report_timeout = REPORT_TIMEOUT_SECONDS if report_timeout is None else report_timeout
    dlr_timeout = DLR_TIMEOUT_SECONDS if dlr_timeout is None else dlr_timeout
    dlr_interval = WHATSAPP_DLR_POLL_INTERVAL_SECONDS if dlr_interval is None else dlr_interval

    result = WhatsAppCampaignDlrResult(campaign_name)
    result.summary.append(("Campaign", campaign_name))

    def fail_generic(step, message):
        result.summary.append((step, "FAIL"))
        result.summary.append(("Final Result", "FAIL"))
        for k, v in result.summary:
            _record(record_property, f"WhatsApp DLR {k}", v)
        print("\n" + result.summary_text())
        raise WhatsAppCampaignDlrError(f"[{campaign_name}] {step}: {message}", summary=result.summary)

    # Campaign found / Report opened
    state = _open_campaign_report(page, campaign_name, report_timeout)
    if state == "not_found":
        fail_generic("Campaign Found", f"campaign '{campaign_name}' could not be found in the Campaigns list")
    result.summary.append(("Campaign Found", "PASS"))

    report_page = WhatsAppCampaignReportPage(page)
    if state != "opened":
        fail_generic("Report Opened", "Reports link could not be opened from the Campaigns list")
    result.summary.append(("Report Opened", "PASS"))

    # Recipients + message ids (retry while the report is still filling in)
    deadline = time.time() + report_timeout
    while True:
        _wait_for_report_rows(report_page, max(5, deadline - time.time()), expected_recipients)
        entries, no_message_id = _collect_recipients_and_message_ids(report_page)
        incomplete = bool(no_message_id) or not entries or (
            expected_recipients is not None and len(entries) < expected_recipients)
        if not incomplete or time.time() >= deadline:
            break
        time.sleep(5)
        report_page.page.reload()

    if not entries and not no_message_id:
        fail_generic("Recipients Collected", "the campaign report lists no recipients")

    result.expected_count = len(entries) + len(no_message_id)
    result.no_message_id = no_message_id
    for label in no_message_id:
        _record(record_property, "WhatsApp DLR no_message_id", label)
    result.summary.append(("Recipients Collected", f"{len(entries)}/{result.expected_count}"))
    for e in entries:
        _record(record_property, f"WhatsApp DLR message_id[{e['recipient']}]", e["message_id"])

    # Per-message DLR polling + schema/correlation verification -- walk
    # EVERY entry, tracking each outcome separately (no fail-fast).
    failure_reports = []
    for e in entries:
        recipient, message_id = e["recipient"], e["message_id"]
        last_response, last_body, events_seen, outcome = poll_for_whatsapp_dlr(
            api_client, message_id, timeout_seconds=dlr_timeout, interval_seconds=dlr_interval,
        )
        _record(record_property, f"WhatsApp DLR events_seen[{message_id}]", events_seen)
        fields = extract_status_fields(last_body) if isinstance(last_body, dict) else {}
        external_message_id = fields.get("external_message_id")
        actual_status = fields.get("status")

        if outcome == "unexpected_response":
            failure_categories = [FAILURE_UNEXPECTED_RESPONSE]
            reason = f"HTTP status={getattr(last_response, 'status_code', None)}"
            report_text = build_failure_report(
                campaign_name=campaign_name, internal_message_id=message_id,
                external_message_id=external_message_id, recipient=recipient,
                actual_status=actual_status, dlr_received=False,
                schema_validation=None, correlation_validation=None,
                failure_categories=failure_categories, failure_reason=reason,
            )
            result.missing.append((recipient, message_id))
            failure_reports.append(report_text)
            continue

        if outcome == "timeout":
            report_text = build_failure_report(
                campaign_name=campaign_name, internal_message_id=message_id,
                external_message_id=external_message_id, recipient=recipient,
                actual_status=actual_status, dlr_received=False,
                schema_validation=None, correlation_validation=None,
                failure_categories=[FAILURE_TIMEOUT],
                failure_reason=f"no DLR observed for this message within {dlr_timeout}s (events seen: {events_seen})",
            )
            result.missing.append((recipient, message_id))
            failure_reports.append(report_text)
            continue

        # outcome is "received" or "rejected" -- a real DLR body exists;
        # validate schema + correlation regardless of which.
        schema_ok, missing_fields, invalid_timestamps = validate_whatsapp_dlr_schema(last_body)
        correlation = correlate_whatsapp_event(last_body, expected_message_id=message_id, expected_recipient=recipient)
        correlation_ok = (
            correlation.get("internal_message_id_match") is True
            and correlation.get("recipient_match") in (True, None)
        )

        failure_categories = []
        reasons = []
        if not schema_ok:
            failure_categories.append(FAILURE_SCHEMA)
            reasons.append(f"missing fields: {missing_fields}")
        if invalid_timestamps:
            failure_categories.append(FAILURE_INVALID_TIMESTAMP)
            reasons.append(f"invalid/missing timestamps: {invalid_timestamps}")
        if correlation.get("internal_message_id_match") is False:
            failure_categories.append(FAILURE_MESSAGE_ID_MISMATCH)
            reasons.append(f"expected message_id={message_id!r}, got {fields.get('internal_message_id')!r}")
        if correlation.get("recipient_match") is False:
            failure_categories.append(FAILURE_RECIPIENT_MISMATCH)
            reasons.append(f"expected recipient={recipient!r}, got {fields.get('recipient_id')!r}")

        if outcome == "rejected":
            # Explicitly rejected is a distinct, reportable outcome from
            # "no DLR yet" -- recorded even when schema/correlation pass,
            # since it is never treated as a successful delivery.
            result.rejected.append((recipient, message_id, last_body))
            if failure_categories:
                report_text = build_failure_report(
                    campaign_name=campaign_name, internal_message_id=message_id,
                    external_message_id=external_message_id, recipient=recipient,
                    actual_status=actual_status, dlr_received=True,
                    schema_validation=schema_ok, correlation_validation=correlation_ok,
                    failure_categories=failure_categories,
                    failure_reason="; ".join(reasons),
                )
                failure_reports.append(report_text)
            continue

        if schema_ok and correlation_ok:
            result.received[message_id] = last_body
            continue

        result.mismatched.append((recipient, message_id, failure_categories))
        report_text = build_failure_report(
            campaign_name=campaign_name, internal_message_id=message_id,
            external_message_id=external_message_id, recipient=recipient,
            actual_status=actual_status, dlr_received=True,
            schema_validation=schema_ok, correlation_validation=correlation_ok,
            failure_categories=failure_categories,
            failure_reason="; ".join(reasons) or "unknown validation failure",
        )
        failure_reports.append(report_text)

    # End-of-run summary (spec section 6: checked/received/missing/mismatched)
    checked = len(entries)
    result.summary.append(("Checked", checked))
    result.summary.append(("Received", len(result.received)))
    result.summary.append(("Missing", len(result.missing)))
    result.summary.append(("Mismatched", len(result.mismatched)))
    result.summary.append(("Rejected", len(result.rejected)))
    result.summary.append(("No Message ID Captured", len(result.no_message_id)))
    for k, v in result.summary:
        _record(record_property, f"WhatsApp DLR {k}", v)

    any_failure = bool(failure_reports) or bool(result.no_message_id)
    if result.no_message_id:
        failure_reports = [
            f"Campaign Name: {campaign_name}\n"
            f"Row: {label}\n"
            f"Failure Categories: {FAILURE_NO_MESSAGE_ID_CAPTURED}\n"
            f"Failure Reason: the Message Details popup never exposed a non-empty "
            f"'Message ID' value for this row -- see module docstring; never silently skipped."
            for label in result.no_message_id
        ] + failure_reports

    result.summary.append(("Final Result", "FAIL" if any_failure else "PASS"))
    _record(record_property, "WhatsApp DLR Final Result", "FAIL" if any_failure else "PASS")
    print("\n" + result.summary_text())
    if failure_reports:
        print("\n\n".join(failure_reports))

    if any_failure:
        combined = (
            f"[{campaign_name}] WhatsApp DLR verification failed for "
            f"{len(failure_reports)} of {checked + len(result.no_message_id)} message(s):\n\n"
            + "\n\n".join(failure_reports)
        )
        raise WhatsAppCampaignDlrError(combined, failure_reports=failure_reports, summary=result.summary)

    return result
