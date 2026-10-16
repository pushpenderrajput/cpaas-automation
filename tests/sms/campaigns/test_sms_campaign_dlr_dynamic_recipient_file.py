
import os
import time
from datetime import datetime, timedelta

import pytest

from pages.sms.campaigns.sms_campaign_page import SMSCampaignPage
from pages.sms.campaigns.sms_campaign_message_report_page import SmsCampaignMessageReportPage
from pages.sms.messaging.sms_message_page import SMSMessagePage
from utils.config import Config
from utils.dlr_format_validator import (
    aggregate_bulk_dlr_validation,
    build_bulk_dlr_validation_report,
)
from utils.sms_dlr_dynamic_recipient import (
    build_failure_table,
    build_summary_text,
    capture_campaign_dlt_template_id,
    capture_campaign_popup_details,
    ceil_to_5min,
    collect_message_records_from_messages_log,
    correlate_message_records,
    extract_message_records,
    floor_to_5min,
    read_and_validate_recipient_file,
    unmatched_dlr_message_ids,
    verify_bulk_dlrs_chunked,
    wait_for_delivery_complete,
)

pytestmark = pytest.mark.sms

VALID_SENDER_ID = Config.SMS_SENDER_ID
VALID_TEMPLATE = Config.SMS_TEMPLATE_NAME

DLR_WAIT_TIMEOUT = int(os.getenv("DLR_WAIT_TIMEOUT", "1800"))
DLR_POLL_INTERVAL = int(os.getenv("DLR_POLL_INTERVAL", "20"))
EXPORT_TIMEOUT = int(os.getenv("SMS_DLR_EXPORT_TIMEOUT", "900"))
VERIFY_TIMEOUT = int(os.getenv("SMS_DLR_VERIFY_TIMEOUT", "180"))
VERIFY_BATCH_SIZE = int(os.getenv("SMS_DLR_VERIFY_BATCH_SIZE", "1000"))


@pytest.fixture
def recipient_file():
    """Section 1: the recipient file is provided dynamically -- never a
    fixed path inside the test. Skips with a clear message if the env
    var is unset or the file doesn't exist, rather than guessing a
    fallback file."""
    path = os.getenv("SMS_DLR_RECIPIENT_FILE")
    if not path:
        pytest.skip(
            "SMS_DLR_RECIPIENT_FILE is not set -- this test requires a "
            "dynamic recipient file path (e.g. recipients_1k.csv / "
            "recipients_10k.csv / recipients_100k.csv) supplied via that "
            "environment variable. Not hardcoded in the test by design."
        )
    if not os.path.isfile(path):
        pytest.skip(f"SMS_DLR_RECIPIENT_FILE points to a file that does not exist: {path}")
    return path


def _create_and_launch_file_upload_campaign(campaign_page, recipient_file_path, name_suffix="DLR_DYN"):
    """Section 3: Name -> Sender ID -> Template (best-effort) -> Import
    Contacts via File Upload (the dynamic recipient file) -> Send Now ->
    Preview -> Launch Campaign -> wait for a success signal. Identical
    sequence to test_sms_campaign_dlr_bulk.py::
    _create_and_launch_send_now_campaign(), with the copy-paste contacts
    step swapped for SMSCampaignPage.import_contacts_from_csv() (the
    confirmed real file-upload flow) so the SAME campaign-creation
    automation handles any recipient-file size without code changes.

    Returns the generated campaign name. Raises AssertionError if launch
    never produces a success signal -- callers must not proceed to DLR
    verification when this fails (per the spec's explicit instruction).
    """
    if not campaign_page.is_campaign_list_page():
        campaign_page.open_campaign_list()
    campaign_page.click_create_campaign()
    campaign_page.page.wait_for_timeout(1000)
    name = f"AutoCamp_{name_suffix}_{int(campaign_page.page.evaluate('() => Date.now()') / 1000)}"
    campaign_page.enter_campaign_name(name)

    campaign_page.select_sender_id(VALID_SENDER_ID)
    try:
        campaign_page.select_template(VALID_TEMPLATE)
    except Exception:
        pass  # template is best-effort, same as TC_C021 / test_sms_campaign_dlr_bulk.py

    # Reuses two independently-confirmed primitives rather than
    # SMSCampaignPage.import_contacts_from_csv() directly: that method
    # never calls select_column_mappings(), so it only works for a
    # single-column (phone-number-only) file. A real uploaded recipient
    # file in this project can carry extra template-variable columns
    # (e.g. "phone", "p1") that trigger column-mapping dropdowns after
    # upload -- upload_contact_file() already drives that mapping step
    # (same auto-select-by-keyword logic SMSCampaignPage itself ships
    # with), so composing click_import_contact() -> upload_contact_file()
    # -> the same duplicate-checkbox + click_import_confirm() steps
    # import_contacts_from_csv() uses covers both single- and
    # multi-column recipient files without a new locator anywhere.
    campaign_page.click_import_contact()
    campaign_page.upload_contact_file(recipient_file_path)
    try:
        cb = campaign_page.page.locator(campaign_page.DUPLICATE_CHECKBOX).first
        if cb.is_checked():
            cb.evaluate("(el) => el.click()")  # keep_duplicates=False
    except Exception:
        pass
    campaign_page.click_import_confirm()

    if campaign_page.is_campaign_list_page():
        return name  # auto-redirected after import = launched

    campaign_page.select_send_now()
    campaign_page.page.wait_for_timeout(1000)

    if campaign_page.is_campaign_list_page():
        return name  # auto-redirected after Send Now = launched

    campaign_page.click_preview()
    assert campaign_page.is_preview_open(), (
        f"Preview modal did not open for campaign '{name}' -- cannot launch."
    )
    campaign_page.page.wait_for_timeout(2000)

    campaign_page.click_launch_campaign()

    deadline = time.time() + 20
    launched = False
    while time.time() < deadline:
        campaign_page.confirm_swal(timeout=1000)
        if campaign_page.is_campaign_list_page():
            launched = True
            break
        if campaign_page.is_element_present(campaign_page.TOAST_SUCCESS, timeout=1000):
            launched = True
            break
        campaign_page.page.wait_for_timeout(500)

    assert launched, (
        f"Campaign '{name}' did not produce a success signal after Launch. "
        f"URL: {campaign_page.get_current_url()}"
    )
    return name


@pytest.mark.smoke
def test_sms_campaign_dlr_dynamic_recipient_file(
    recipient_file, api_client, logged_in_page, record_property
):
    recipient_file_name = os.path.basename(recipient_file)
    record_property("Recipient File", recipient_file_name)

    # ── Section 1-2: Dynamic Recipient File / Validate Input Before Campaign ─
    file_result = read_and_validate_recipient_file(recipient_file)
    expected_recipient_count = file_result.expected_recipient_count

    record_property("Input Recipients (unique)", str(expected_recipient_count))
    record_property("Input Rows (total)", str(file_result.total_rows))
    record_property("Duplicate Numbers Dropped", str(file_result.duplicate_count))
    record_property("Invalid-Format Numbers", str(len(file_result.invalid_numbers)))
    print(
        f"Recipient File: {recipient_file_name} / "
        f"Expected Recipients: {expected_recipient_count}"
    )

    assert expected_recipient_count > 0, (
        f"Recipient file '{recipient_file}' produced 0 valid, unique recipients "
        f"after format validation and de-duplication -- cannot launch a campaign. "
        f"Invalid numbers (first 20): {file_result.invalid_numbers[:20]}"
    )
    assert not file_result.invalid_numbers, (
        f"Recipient file '{recipient_file}' contains "
        f"{len(file_result.invalid_numbers)} number(s) that fail the project's "
        f"recipient-number format rule (digits only, 7-15 chars): "
        f"{file_result.invalid_numbers[:20]}"
    )

    # ── Section 3: Create and Send SMS Campaign ─────────────────────────────
    campaign_page = SMSCampaignPage(logged_in_page)
    campaign_page.open_campaign_list()
    run_start_dt = datetime.now()
    campaign_name = _create_and_launch_file_upload_campaign(campaign_page, recipient_file)
    record_property("campaign_name", campaign_name)

    if not campaign_page.is_campaign_list_page():
        campaign_page.open_campaign_list()
    campaign_page.search(campaign_name)
    found = campaign_page.is_campaign_name_in_list(campaign_name, timeout=15000)
    assert found, f"Campaign '{campaign_name}' could not be found in the Campaigns list after search."

    opened = campaign_page.click_reports_link_on_first_row()
    assert opened, f"Reports link could not be opened for campaign '{campaign_name}'."

    report_page = SmsCampaignMessageReportPage(logged_in_page)
    assert report_page.is_report_page(), (
        f"Did not land on the campaign message report page for "
        f"'{campaign_name}'. URL: {report_page.get_current_url()}"
    )
    report_page.wait_for_table_load()
    campaign_id = report_page.get_campaign_id_from_url()
    assert campaign_id, (
        f"Could not extract the campaign id from the report page URL: "
        f"{report_page.get_current_url()}"
    )

    # ── Section 4: Wait Until Messages Are Delivered (polled, not a fixed
    #               sleep) ──────────────────────────────────────────────────
    delivery_complete, last_pending_count = wait_for_delivery_complete(
        report_page, campaign_id, DLR_WAIT_TIMEOUT, DLR_POLL_INTERVAL,
        record_property=record_property,
    )
    run_end_dt = datetime.now()
    record_property("Delivery complete before timeout", str(delivery_complete))
    record_property("Pending (DLR Awaited) rows (final)", str(last_pending_count))
    if not delivery_complete:
        print(
            f"WARNING: the Pending (DLR Awaited) filtered view did not reach "
            f"0 rows within {DLR_WAIT_TIMEOUT}s (last observed: "
            f"{last_pending_count} row(s) still pending). Proceeding to "
            f"export/DLR verification anyway -- any still-undelivered "
            f"messages will surface as missing/unreceived DLRs below rather "
            f"than silently passing."
        )

    # ── Section 5-7: Export Campaign Report / Verify Recipient Count /
    #                 Dynamic Message ID Collection ─────────────────────────
    # Per the project owner's explicit instruction: once nothing is left
    # in Sent/Pending (Section 4 above), go to the SMS Messages Log, apply
    # the DLT Template ID (captured dynamically from the Campaign Report's
    # own View popup -- never hardcoded) + Sender ID + Source=Campaign +
    # Status in {DELIVRD, FAILED, REJECTED} + a precise Received From/To
    # window covering this run, then export that filtered log (its own
    # export is confirmed to carry Message Id, unlike the Campaign
    # Report's own export).
    dlt_template_id, msg_prefix = capture_campaign_popup_details(report_page, campaign_id)
    record_property("DLT Template ID (captured)", dlt_template_id)
    if msg_prefix:
        record_property("Message ID Batch Prefix (captured)", msg_prefix)

    from_dt = floor_to_5min(run_start_dt)
    to_dt = ceil_to_5min(run_end_dt)
    record_property("Messages Log Filter From", from_dt.strftime("%Y-%m-%d %H:%M"))
    record_property("Messages Log Filter To", to_dt.strftime("%Y-%m-%d %H:%M"))

    download_path, export_headers, matched_rows = collect_message_records_from_messages_log(
        SMSMessagePage(logged_in_page),
        dlt_template_id,
        VALID_SENDER_ID,
        from_dt,
        to_dt,
        export_timeout_ms=EXPORT_TIMEOUT * 1000,
        expected_recipients=file_result.unique_recipients,
        message_id_prefix=msg_prefix,
    )
    record_property("Export File", download_path)
    record_property("Campaign Export Records", str(len(matched_rows)))
    print(f"Campaign Export Records: {len(matched_rows)}")

    # ── Section 6: Verify Recipient Count before proceeding to DLR check ────
    export_record_count = len(matched_rows)
    assert export_record_count == expected_recipient_count, (
        f"Campaign report export does not contain the expected number of "
        f"records for campaign '{campaign_name}'.\n"
        f"Expected: {expected_recipient_count}\n"
        f"Export: {export_record_count}\n"
        f"Difference: {export_record_count - expected_recipient_count}\n"
        f"(Input file had {file_result.duplicate_count} duplicate and "
        f"{len(file_result.invalid_numbers)} invalid-format number(s) already "
        f"excluded from Expected above -- a further mismatch is not explained "
        f"by those and must not be silently assumed away.)"
    )

    message_records = extract_message_records(matched_rows, export_headers)
    record_property("Message IDs collected", str(len(message_records)))

    # ── Section 8: Verify DLRs via the existing DLR Receiver ────────────────
    message_ids = [r["message_id"] for r in message_records]
    dlr_results, raw_bodies = verify_bulk_dlrs_chunked(
        api_client, message_ids, VERIFY_BATCH_SIZE, VERIFY_TIMEOUT,
        interval_seconds=min(5, VERIFY_TIMEOUT),
    )
    for body in raw_bodies:
        record_property(
            f"DLR verify chunk[{body['chunk_start']}:{body['chunk_start'] + body['chunk_size']}] status",
            str(body["status_code"]),
        )

    # ── DLR Format/Schema Verification -- for EVERY received DLR, on top
    #               of presence/correlation (Section 9-10 below). Generic
    #               test: status/code are only checked to EXIST, never
    #               compared to a specific value. recipient numbers come
    #               dynamically from the exported message_records, never
    #               hard-coded ──────────────────────────────────────────
    expected_mobiles = {r["message_id"]: r["recipient"] for r in message_records}
    dlr_format_counts, dlr_format_failures = aggregate_bulk_dlr_validation(
        message_ids, dlr_results, expected_mobiles=expected_mobiles
    )
    dlr_format_report = build_bulk_dlr_validation_report(dlr_format_counts)
    record_property("DLR Format Validation Report", dlr_format_report)
    print("\n" + dlr_format_report)

    # ── Section 9-10: Recipient-to-DLR Correlation / Complete DLR Coverage ──
    correlation = correlate_message_records(message_records, dlr_results)
    unmatched_ids = unmatched_dlr_message_ids(dlr_results, message_records)

    missing = correlation["missing"]
    not_received = correlation["not_received"]
    recipient_mismatch = correlation["recipient_mismatch"]
    duplicate_message_ids = correlation["duplicate_message_ids"]
    verified = correlation["verified"]

    expected_dlr_count = len(message_records)
    received_dlr_count = len(verified)
    missing_dlr_count = len(missing) + len(not_received)

    record_property("Expected DLRs", str(expected_dlr_count))
    record_property("Received DLRs", str(received_dlr_count))
    record_property("Missing DLRs", str(missing_dlr_count))
    record_property("Unmatched Message IDs", str(len(unmatched_ids)))
    record_property("Recipient Mismatches", str(len(recipient_mismatch)))
    record_property("Duplicate Message IDs", str(len(duplicate_message_ids)))

    # ── Section 11: Final Validation ─────────────────────────────────────────
    dlr_format_passed = (
        dlr_format_counts["invalid_format"] == 0
        and dlr_format_counts["message_id_mismatches"] == 0
        and dlr_format_counts["mobile_mismatches"] == 0
    )
    record_property("DLR Format Valid", str(dlr_format_passed))

    passed = (
        expected_recipient_count == export_record_count == expected_dlr_count == received_dlr_count
        and not missing
        and not not_received
        and not unmatched_ids
        and not recipient_mismatch
        and not duplicate_message_ids
        and dlr_format_passed
    )

    failure_rows = []
    for rec in missing:
        failure_rows.append((rec["message_id"], rec["recipient"], "No DLR entry returned for this message_id"))
    for rec in not_received:
        failure_rows.append((rec["message_id"], rec["recipient"], "DLR entry present but received=false"))
    for rec in recipient_mismatch:
        failure_rows.append(
            (rec["message_id"], rec["recipient"],
             f"DLR recipient does not match campaign recipient (DLR reported {rec.get('dlr_recipient')!r})")
        )
    for mid in unmatched_ids:
        failure_rows.append((mid, "", "DLR entry returned for a message_id never exported for this campaign"))
    for mid in duplicate_message_ids:
        dup_recipients = [r["recipient"] for r in message_records if r["message_id"] == mid]
        failure_rows.append((mid, ", ".join(dup_recipients), "message_id appears on more than one exported record"))
    for mid, recipient, reason in dlr_format_failures:
        failure_rows.append((mid, recipient, reason))

    # ── Section 15: Failure Report (exact required format) ──────────────────
    summary_text = build_summary_text(
        recipient_file_name,
        expected_recipient_count,
        export_record_count,
        expected_dlr_count,
        received_dlr_count,
        missing_dlr_count,
        len(unmatched_ids),
        len(recipient_mismatch),
        len(duplicate_message_ids),
        "PASS" if passed else "FAIL",
    )
    if failure_rows:
        summary_text += "\n\n" + build_failure_table(failure_rows)

    # DLR format/schema aggregate report is reported IN ADDITION to the
    # exact Section 15 summary format above (which stays untouched).
    summary_text += "\n\n" + dlr_format_report

    print(summary_text)
    record_property("Summary", summary_text)

    assert passed, summary_text
