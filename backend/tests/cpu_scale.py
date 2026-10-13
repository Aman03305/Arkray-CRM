"""CPU-time bounds that hold on a slower, loaded or traced machine without losing their teeth.

The upload checks' cost tests (activities/tests/test_upload_parser_costs.py and the others that
bound an upload check's CPU) were calibrated on the development laptop. Under coverage, which
traces the arkray code being timed (its `source` is arkray, not the tests), that code runs 2-4
times slower, and a CI runner of two shared vCPUs is slower again, so an absolute bound fails
for nothing. `bound(seconds)` scales a laptop bound by how much slower this process runs a fixed
workload of arkray code right now: the same kind of code the tests time (core.text.clean_line's
per-character Python loop, an xlsx check that parses and judges formulas, a CSV check that walks
and judges cells), so tracing, a slower CPU or a loaded one slow it as they slow the code under
test. The factor is never below 1: on the laptop the bounds are what they were.

The teeth: what these tests catch is a cost that grows with the input (a quadratic judge, a
per-chunk re-translation, a pass the units don't price), ten to a hundred times the linear cost
at the sizes tested, far past a factor of 2-4. Where a control exists, a test compares with it
instead (the same work on ordinary text), which needs no scaling at all."""

from __future__ import annotations

import io
import statistics
import time
import zipfile

from arkray.activities import storage
from arkray.core.text import clean_line

# The workload's process CPU on the development laptop (an i7-13700HX), without coverage: the
# fastest of 20 runs (measured 2026-10-13: 0.109-0.19 s, median 0.125; under coverage about 3.5
# times that). A run at that speed has factor 1.
REFERENCE_SECONDS = 0.11
RECALIBRATE_SECONDS = 10.0  # how long a measured factor is used before it is measured again

_TEXT = "Asha Fernández, अनीता शर्मा, Lab 12 / Pune; call back Monday. " * 300
_SM = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"


def _workbook() -> bytes:
    """An ordinary workbook whose sheet is parsed (a comment) and holds 1,500 formulas."""
    types = (
        f"<Types xmlns='{_CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Override PartName='/xl/workbook.xml' ContentType='{_MAIN}'/>"
        f"<Override PartName='/xl/worksheets/sheet1.xml' ContentType='{_SHEET}'/></Types>"
    )
    cells = "".join(
        f"<c r='A{n}'><f>SUM(B{n}:D{n})+IF(E{n}&gt;0,E{n},0)</f></c>" for n in range(1, 1501)
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as built:
        built.writestr("[Content_Types].xml", types)
        built.writestr("xl/workbook.xml", f"<workbook xmlns='{_SM}'/>")
        built.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet xmlns='{_SM}'><!----><sheetData><row r='1'>{cells}</row></sheetData>"
            "</worksheet>",
        )
    return out.getvalue()


_WORKBOOK = _workbook()
# A call log whose every row mentions a call (so it is walked and its candidates judged).
_CSV = (
    "Date,Lead,Activity,Notes\n" + "2026-10-12,Asha Lab,Call(s),=SUM(1;2) next week\n" * 1500
).encode()


def _workload() -> float:
    started = time.process_time()
    for _ in range(8):
        clean_line(_TEXT)
    for _ in range(5):
        storage.CATALOG["xlsx"].recognise(_WORKBOOK[:8192], io.BytesIO(_WORKBOOK))
        storage.CATALOG["csv"].recognise(_CSV[:8192], io.BytesIO(_CSV))
    return time.process_time() - started


_measured: tuple[float, float] | None = None  # (when, factor)


def slowdown() -> float:
    """How many times slower than the laptop this process runs the workload now (the median of
    three runs), never below 1; measured again once the last measurement is RECALIBRATE_SECONDS
    old. The tests call it from an autouse fixture, at setup: before a test patches anything the
    workload runs through (a spy on storage._stream_story would count the workload's parses)."""
    global _measured
    now = time.monotonic()
    if _measured is None or now - _measured[0] > RECALIBRATE_SECONDS:
        factor = statistics.median(_workload() for _ in range(3)) / REFERENCE_SECONDS
        _measured = (time.monotonic(), max(1.0, factor))
    return _measured[1]


def bound(seconds: float) -> float:
    """A CPU bound calibrated on the laptop, scaled by the factor measured last (at the test's
    setup), so nothing is measured while a test has storage patched."""
    return seconds * (_measured[1] if _measured is not None else slowdown())
