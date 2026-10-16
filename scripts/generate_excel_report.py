#!/usr/bin/env python3
"""
scripts/generate_excel_report.py

Converts a pytest JUnit XML report (already produced by the run via
pytest.ini's --junitxml / scripts/run_tests.py's own --junitxml
resolution) into an .xlsx report, so every test run gets an Excel
version of the results alongside the existing HTML/JUnit reports --
no new way of running tests, no change to what pytest itself produces.

Usage (standalone):
    python scripts/generate_excel_report.py reports/junit.xml reports/test_report.xlsx

Usage (library, called automatically by scripts/run_tests.py after
every run, pass or fail):
    from scripts.generate_excel_report import generate_excel_report
    generate_excel_report(junit_path, xlsx_path)

Never raises out of generate_excel_report() for anything caused by the
JUnit file itself (missing, unparsable, empty) -- a report-generation
problem must never fail, mask, or change the exit code of the actual
test run that already finished. scripts/run_tests.py calls this AFTER
deciding its own return code and only prints a warning on failure here.

Sheets produced:
  Summary     -- totals (passed/failed/error/skipped/reruns), pass rate,
                 total duration, run metadata (ENV/INSTANCE/timestamp if
                 available from the JUnit <testsuite> attributes).
  Results     -- one row per test case: channel, classname, test name,
                 status, duration (s), failure/error message (first line
                 of the real pytest failure text, never fabricated).
  By Channel  -- pass/fail/skip counts grouped by the tests/<channel>
                 folder a test lives under (parsed from its classname),
                 so a multi-channel run (sms/rcs/whatsapp/email/common)
                 is readable at a glance.

Status values: Passed, Failed, Error, Skipped, Passed (after rerun) --
the last one is pytest-rerunfailures' own case (see
scripts/run_tests.py's module docstring on --reruns): a test that failed
on an earlier attempt but passed on its FINAL attempt. JUnit XML from
recent pytest/pytest-rerunfailures versions represents a completed rerun
attempt as a <rerun> child element (distinct from <failure>/<error>) on
the SAME <testcase> that also carries the final outcome -- never a
separate passing <testcase>, so this never double-counts.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
except ImportError:  # pragma: no cover -- see module docstring: never fail the test run over this
    openpyxl = None


FONT_NAME = "Arial"


def _styles():
    return {
        "title_font": Font(name=FONT_NAME, size=16, bold=True, color="FFFFFF"),
        "section_font": Font(name=FONT_NAME, size=12, bold=True, color="FFFFFF"),
        "header_font": Font(name=FONT_NAME, size=10, bold=True, color="FFFFFF"),
        "body_font": Font(name=FONT_NAME, size=10),
        "body_font_bold": Font(name=FONT_NAME, size=10, bold=True),
        "note_font": Font(name=FONT_NAME, size=9, italic=True, color="666666"),
        "navy": PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid"),
        "blue": PatternFill(start_color="2E5597", end_color="2E5597", fill_type="solid"),
        "green": PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid"),
        "red": PatternFill(start_color="FCE4E4", end_color="FCE4E4", fill_type="solid"),
        "yellow": PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"),
        "grey": PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid"),
        "border": Border(*(Side(style="thin", color="BFBFBF"),) * 4),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "center": Alignment(horizontal="center", vertical="center"),
        "wrap_center": Alignment(wrap_text=True, vertical="center"),
    }


# ── JUnit parsing ───────────────────────────────────────────────────────────

def _parse_junit(junit_path: str):
    """Returns (suite_attrs: dict, cases: list[dict]). `suite_attrs` is the
    raw attribute dict of the FIRST <testsuite> element (name/tests/
    failures/errors/skipped/time/timestamp -- whichever pytest actually
    wrote; never invented). `cases` is one dict per <testcase>:
    {classname, name, time, status, message}. Raises on a genuinely
    unreadable/unparsable file -- the caller decides how to handle that,
    this function never silently returns an empty result for a file that
    exists but is malformed, so a truncated/corrupt JUnit file is visible
    rather than reported as "0 tests"."""
    tree = ET.parse(junit_path)
    root = tree.getroot()

    # pytest's own junitxml root is <testsuites><testsuite>...</testsuite></testsuites>
    # in every version this project's requirements.txt pins (pytest==8.2.0);
    # support a bare <testsuite> root too, defensively, since older/other
    # JUnit writers sometimes skip the outer element.
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    suite_attrs = dict(suites[0].attrib) if suites else {}

    cases = []
    for suite in suites:
        for tc in suite.findall("testcase"):
            status = "Passed"
            message = ""

            skipped_el = tc.find("skipped")
            failure_el = tc.find("failure")
            error_el = tc.find("error")
            rerun_els = tc.findall("rerun")

            if skipped_el is not None:
                status = "Skipped"
                message = (skipped_el.get("message") or (skipped_el.text or "")).strip()
            elif failure_el is not None:
                status = "Failed"
                message = (failure_el.get("message") or (failure_el.text or "")).strip()
            elif error_el is not None:
                status = "Error"
                message = (error_el.get("message") or (error_el.text or "")).strip()
            elif rerun_els:
                status = "Passed (after rerun)"
                last_rerun = rerun_els[-1]
                message = (last_rerun.get("message") or (last_rerun.text or "")).strip()

            # Keep only the message's first line -- the full traceback
            # already lives in the HTML report; this sheet is a scan view,
            # not a replacement for it.
            message = message.splitlines()[0].strip() if message else ""

            cases.append({
                "classname": tc.get("classname", ""),
                "name": tc.get("name", ""),
                "time": float(tc.get("time", "0") or 0),
                "status": status,
                "message": message,
            })
    return suite_attrs, cases


def _channel_from_classname(classname: str) -> str:
    """"tests.whatsapp.campaigns.test_x" -> "whatsapp" -- same convention
    scripts/run_tests.py's own _channel_label()/conftest.py's
    _channel_from_args() use, applied to a testcase's classname instead
    of a forwarded CLI path. Returns "other" if the classname doesn't
    start with "tests." (never fabricates a channel name)."""
    parts = classname.split(".")
    if len(parts) > 1 and parts[0] == "tests":
        return parts[1]
    return "other"


# ── Workbook building ───────────────────────────────────────────────────────

def _build_workbook(suite_attrs: dict, cases: list, meta: dict):
    s = _styles()
    wb = openpyxl.Workbook()

    total = len(cases)
    passed = sum(1 for c in cases if c["status"] in ("Passed", "Passed (after rerun)"))
    reran = sum(1 for c in cases if c["status"] == "Passed (after rerun)")
    failed = sum(1 for c in cases if c["status"] == "Failed")
    errored = sum(1 for c in cases if c["status"] == "Error")
    skipped = sum(1 for c in cases if c["status"] == "Skipped")
    total_time = sum(c["time"] for c in cases)
    pass_rate = (passed / total * 100) if total else 0.0

    # ── Summary ──────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCD", [26, 30, 20, 20]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:D1")
    ws["A1"] = "Pytest Run Report"
    ws["A1"].font = s["title_font"]
    ws["A1"].fill = s["navy"]
    ws.row_dimensions[1].height = 28

    row = 3
    for label, value in [
        ("Run at", meta.get("run_at", "")),
        ("ENV", meta.get("env", "")),
        ("INSTANCE", meta.get("instance", "")),
        ("JUnit source", meta.get("junit_path", "")),
        ("Suite name", suite_attrs.get("name", "")),
    ]:
        ws.cell(row=row, column=1, value=label).font = s["body_font_bold"]
        c = ws.cell(row=row, column=2, value=value)
        c.font = s["body_font"]
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
        row += 1

    row += 1
    ws.merge_cells(f"A{row}:D{row}")
    ws.cell(row=row, column=1, value="Results").font = s["section_font"]
    ws.cell(row=row, column=1).fill = s["blue"]
    ws.row_dimensions[row].height = 22
    row += 1

    headers = ["Metric", "Count", "", ""]
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=1 + i, value=h)
        c.font = s["header_font"]
        c.fill = s["blue"]
        c.border = s["border"]
    ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
    row += 1

    metrics = [
        ("Total tests", total, s["grey"]),
        ("Passed (first try)", passed - reran, s["green"]),
        ("Passed (after rerun)", reran, s["yellow"]),
        ("Failed", failed, s["red"]),
        ("Error", errored, s["red"]),
        ("Skipped", skipped, s["yellow"]),
        ("Pass rate", f"{pass_rate:.1f}%", s["green"] if pass_rate == 100 else s["yellow"]),
        ("Total duration (s)", f"{total_time:.1f}", s["grey"]),
    ]
    for label, value, fill in metrics:
        ws.cell(row=row, column=1, value=label).font = s["body_font_bold"]
        ws.cell(row=row, column=1).border = s["border"]
        c = ws.cell(row=row, column=2, value=value)
        c.font = s["body_font"]
        c.fill = fill
        c.alignment = s["center"]
        c.border = s["border"]
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        ws.row_dimensions[row].height = 20
        row += 1

    # ── Results ──────────────────────────────────────────────────────────
    ws2 = wb.create_sheet("Results")
    ws2.sheet_view.showGridLines = False
    for col, w in zip("ABCDE", [16, 55, 40, 14, 60]):
        ws2.column_dimensions[col].width = w

    hdrs = ["Channel", "Test", "Class", "Status", "Duration (s)", "Message"]
    widths = [14, 45, 45, 18, 12, 60]
    for col_letter, w in zip("ABCDEF", widths):
        ws2.column_dimensions[col_letter].width = w
    for i, h in enumerate(hdrs, start=1):
        c = ws2.cell(row=1, column=i, value=h)
        c.font = s["header_font"]
        c.fill = s["blue"]
        c.border = s["border"]
        c.alignment = s["wrap_center"]
    ws2.row_dimensions[1].height = 24
    ws2.freeze_panes = "A2"

    status_fill = {
        "Passed": s["green"],
        "Passed (after rerun)": s["yellow"],
        "Failed": s["red"],
        "Error": s["red"],
        "Skipped": s["yellow"],
    }

    r = 2
    for c in sorted(cases, key=lambda x: (_channel_from_classname(x["classname"]), x["classname"], x["name"])):
        channel = _channel_from_classname(c["classname"])
        vals = [channel, c["name"], c["classname"], c["status"], round(c["time"], 2), c["message"]]
        for i, v in enumerate(vals, start=1):
            cell = ws2.cell(row=r, column=i, value=v)
            cell.font = s["body_font"]
            cell.border = s["border"]
            cell.alignment = s["wrap"]
            if i == 4:
                cell.fill = status_fill.get(c["status"], s["grey"])
                cell.alignment = s["center"]
        r += 1

    # ── By Channel ───────────────────────────────────────────────────────
    ws3 = wb.create_sheet("By Channel")
    ws3.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFG", [18, 12, 12, 12, 12, 12, 14]):
        ws3.column_dimensions[col].width = w

    by_channel: dict = {}
    for c in cases:
        ch = _channel_from_classname(c["classname"])
        d = by_channel.setdefault(ch, {"Passed": 0, "Passed (after rerun)": 0, "Failed": 0, "Error": 0, "Skipped": 0})
        d[c["status"]] = d.get(c["status"], 0) + 1

    hdrs3 = ["Channel", "Total", "Passed", "After Rerun", "Failed", "Error", "Skipped"]
    for i, h in enumerate(hdrs3, start=1):
        cell = ws3.cell(row=1, column=i, value=h)
        cell.font = s["header_font"]
        cell.fill = s["blue"]
        cell.border = s["border"]
    ws3.row_dimensions[1].height = 22
    ws3.freeze_panes = "A2"

    r = 2
    for ch in sorted(by_channel):
        d = by_channel[ch]
        tot = sum(d.values())
        row_vals = [ch, tot, d["Passed"], d["Passed (after rerun)"], d["Failed"], d["Error"], d["Skipped"]]
        for i, v in enumerate(row_vals, start=1):
            cell = ws3.cell(row=r, column=i, value=v)
            cell.font = s["body_font_bold"] if i == 1 else s["body_font"]
            cell.border = s["border"]
            cell.alignment = s["center"] if i > 1 else Alignment(vertical="center")
            if i == 5 and v:  # Failed
                cell.fill = s["red"]
            elif i == 6 and v:  # Error
                cell.fill = s["red"]
            elif i == 4 and v:  # After rerun
                cell.fill = s["yellow"]
        r += 1

    return wb


# ── Public entry point ──────────────────────────────────────────────────────

def generate_excel_report(junit_path: str, xlsx_path: str, meta: dict | None = None) -> bool:
    """Reads `junit_path`, writes `xlsx_path`. Returns True on success,
    False on any failure (missing openpyxl, missing/corrupt junit file,
    write error) -- NEVER raises, per the module docstring: a report-
    generation problem must never affect the test run's own exit code.
    Prints a one-line reason to stderr on failure so it's visible in CI
    logs without being mistaken for a test failure."""
    meta = dict(meta or {})
    meta.setdefault("junit_path", junit_path)
    if openpyxl is None:
        print(
            "[generate_excel_report] WARNING: openpyxl not installed -- skipping "
            "Excel report (requirements.txt already pins openpyxl==3.1.2; "
            "reinstall if this environment is missing it).",
            file=sys.stderr,
        )
        return False
    if not os.path.isfile(junit_path):
        print(f"[generate_excel_report] WARNING: no JUnit file at {junit_path!r} -- skipping Excel report.", file=sys.stderr)
        return False
    try:
        suite_attrs, cases = _parse_junit(junit_path)
    except ET.ParseError as e:
        print(f"[generate_excel_report] WARNING: could not parse {junit_path!r} ({e}) -- skipping Excel report.", file=sys.stderr)
        return False

    try:
        wb = _build_workbook(suite_attrs, cases, meta)
        os.makedirs(os.path.dirname(os.path.abspath(xlsx_path)), exist_ok=True)
        wb.save(xlsx_path)
    except Exception as e:  # pragma: no cover -- defensive, see docstring
        print(f"[generate_excel_report] WARNING: failed to write {xlsx_path!r} ({e}) -- skipping Excel report.", file=sys.stderr)
        return False

    print(f"[generate_excel_report] Excel report written: {xlsx_path}")
    return True


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("Usage: python scripts/generate_excel_report.py <junit.xml> <output.xlsx>", file=sys.stderr)
        return 2
    ok = generate_excel_report(argv[0], argv[1])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
