"""Red tests: formula-injection guard on the PATH writers (crosswalk_to_csv / _xlsx).

Gap closed: test_security_phase1.py feeds formula payloads only to the in-memory
bytes writers (crosswalk_csv_bytes / crosswalk_xlsx_bytes), and test_review.py's
byte-identity tests use the clean sample policy. So a guard placed only in the bytes
writers would leave the suite green while the path writers (and the CLI that calls
them) still emit live formulas. These tests pin the guard on the path writers.

Convention (same rule as test_security_phase1.py):
  CSV   no cell may start with = + - @ TAB or CR; the payload text must still be in
        the cell (neutralised, e.g. a leading single quote -- not dropped).
  XLSX  no cell may be a live formula (data_type "f") except the Summary sheet's own
        COUNTIF/COUNTIFS; a cell starting with a trigger is acceptable only when it
        is stored as a string (data_type "s"). Payload text must still be present.
        (openpyxl turns CR into LF on read-back, so CR payloads compare with LF.)
  Clean cells (no leading trigger, including text with trigger chars mid-string)
  must come out byte-for-byte unchanged.

Inputs are built directly (PolicyStatement / Mapping / MappingResult), not via
parse_policy, because parse_policy strips leading TAB/CR from statements. Offline,
no model, no network; everything is written under pytest's tmp dir.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

from control_mapper.crosswalk import CrosswalkInput, build_crosswalk  # noqa: E402
from control_mapper.export import crosswalk_to_csv, crosswalk_to_xlsx  # noqa: E402
from control_mapper.frameworks import load_framework  # noqa: E402
from control_mapper.models import (  # noqa: E402
    CoverageLevel,
    Mapping,
    MappingResult,
    PolicyStatement,
)

NIST_JSON = ROOT / "data" / "frameworks" / "nist_800-53_subset.json"
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
TRIGGER_IDS = ["eq", "plus", "minus", "at", "tab", "cr"]
FIELDS = ("statement", "rationale", "title")
CSV_COLUMN = {"statement": "statement_text", "rationale": "rationale",
              "title": "control_title"}

# Clean text deliberately contains trigger characters mid-string: only a LEADING
# trigger is dangerous, so these must pass through untouched.
CLEAN_STATEMENT = "Remote access requires MFA; ratio a-b=c @ HQ."
CLEAN_RATIONALE = "Directly requires MFA (score = 1, see @policy)."
CLEAN_TITLE = "Clean Control Title + Notes"
CLEAN_CONFIDENCE = 0.9

# Mapped controls: rows 0..5 carry one trigger each, row 6 is the clean row.
CONTROLS = ["AC-2", "AC-3", "AC-6", "AC-17", "AU-2", "AU-6", "AT-2"]
GAP_PAYLOAD_CONTROL = "CM-2"          # uncovered -> appears on the Gaps sheet
GAP_PAYLOAD_TITLE = '=HYPERLINK("http://x","gap")'


def payload(trigger: str, field: str) -> str:
    return f"{trigger}1+cmd|' /C calc'!A0 [{field}]"


def _xlsx_form(text: str) -> str:
    return text.replace("\r", "\n")


@pytest.fixture(scope="module")
def written(tmp_path_factory):
    work = tmp_path_factory.mktemp("path_guard")
    data = json.loads(NIST_JSON.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in data["controls"]}
    assert set(CONTROLS + [GAP_PAYLOAD_CONTROL]) <= set(by_id), "NIST subset changed"
    for i, trig in enumerate(DANGEROUS):
        by_id[CONTROLS[i]]["title"] = payload(trig, "title")
    by_id[CONTROLS[6]]["title"] = CLEAN_TITLE
    by_id[GAP_PAYLOAD_CONTROL]["title"] = GAP_PAYLOAD_TITLE
    clean_gap = next(c for c in data["controls"]
                     if c["id"] not in CONTROLS + [GAP_PAYLOAD_CONTROL])
    fw_path = work / "nist_payload.json"
    fw_path.write_text(json.dumps(data), encoding="utf-8")
    fw = load_framework(fw_path)

    statements = [PolicyStatement(id=f"P{i + 1}", text=payload(t, "statement"))
                  for i, t in enumerate(DANGEROUS)]
    statements.append(PolicyStatement(id="P7", text=CLEAN_STATEMENT))
    rationales = [payload(t, "rationale") for t in DANGEROUS] + [CLEAN_RATIONALE]
    result = MappingResult(framework_id=fw.framework_id, mappings=[
        Mapping(statement_id=f"P{i + 1}", control_id=cid, coverage=CoverageLevel.FULL,
                confidence=CLEAN_CONFIDENCE, rationale=rationales[i])
        for i, cid in enumerate(CONTROLS)
    ])
    inputs = [CrosswalkInput(fw, result)]
    cw = build_crosswalk(statements, inputs)

    csv_path, xlsx_path = work / "out.csv", work / "out.xlsx"
    n = crosswalk_to_csv(cw, statements, inputs, csv_path)
    crosswalk_to_xlsx(cw, statements, inputs, xlsx_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    assert n == len(CONTROLS) == len(rows) - 1
    return {
        "header": rows[0],
        "rows": rows[1:],
        "dict_rows": {r["control_id"]: r for r in (dict(zip(rows[0], x)) for x in rows[1:])},
        "wb": load_workbook(xlsx_path),
        "fw": fw,
        "clean_gap": clean_gap,
    }


# ---------------------------------------------------------------- CSV


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("trigger", DANGEROUS, ids=TRIGGER_IDS)
def test_path_csv_payload_cell_is_guarded(written, trigger, field):
    i = DANGEROUS.index(trigger)
    cell = written["dict_rows"][CONTROLS[i]][CSV_COLUMN[field]]
    assert not cell.startswith(DANGEROUS), f"unguarded {field} cell: {cell!r}"
    assert payload(trigger, field) in cell, f"payload dropped, not neutralised: {cell!r}"


def test_path_csv_no_cell_anywhere_starts_with_a_trigger(written):
    bad = [c for row in written["rows"] for c in row if c.startswith(DANGEROUS)]
    assert not bad, f"formula-trigger cells in path CSV: {bad!r}"


def test_path_csv_clean_row_is_unchanged(written):
    row = written["dict_rows"][CONTROLS[6]]
    assert row == {
        "statement_id": "P7",
        "statement_text": CLEAN_STATEMENT,
        "framework_id": written["fw"].framework_id,
        "control_id": CONTROLS[6],
        "control_title": CLEAN_TITLE,
        "coverage": "full",
        "confidence": str(CLEAN_CONFIDENCE),
        "rationale": CLEAN_RATIONALE,
    }
    # identifiers of the payload rows are untouched too (e.g. "AC-2" keeps its dash)
    assert set(written["dict_rows"]) == set(CONTROLS)


# ---------------------------------------------------------------- XLSX


def _xlsx_offenders(wb):
    """-> (texts, offenders, intended Summary formulas). Same rule as phase-1 scan."""
    texts, offenders, summary_formulas = [], [], []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if not isinstance(v, str):
                    continue
                texts.append(v)
                if (ws.title == "Summary" and cell.data_type == "f"
                        and v.startswith(("=COUNTIF(", "=COUNTIFS("))):
                    summary_formulas.append(v)
                elif cell.data_type == "f":
                    offenders.append((ws.title, cell.coordinate, v))
    return texts, offenders, summary_formulas


def _mappings_row(wb, control_id):
    ws = wb["Mappings"]
    header = [c.value for c in ws[1]]
    col = header.index("control_id")
    hits = [r for r in ws.iter_rows(min_row=2) if r[col].value == control_id]
    assert len(hits) == 1, f"{control_id} rows in Mappings: {len(hits)}"
    return dict(zip(header, hits[0]))


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("trigger", DANGEROUS, ids=TRIGGER_IDS)
def test_path_xlsx_payload_cell_is_guarded(written, trigger, field):
    i = DANGEROUS.index(trigger)
    cell = _mappings_row(written["wb"], CONTROLS[i])[CSV_COLUMN[field]]
    v = cell.value
    assert isinstance(v, str)
    # A loaded str cell is either "s" (string) or "f" (formula); "s" is safe in XLSX.
    assert cell.data_type == "s", f"{field} stored as {cell.data_type!r}: {v!r}"
    assert _xlsx_form(payload(trigger, field)) in v, f"payload dropped: {v!r}"


def test_path_xlsx_no_live_formula_except_summary_counts(written):
    texts, offenders, summary_formulas = _xlsx_offenders(written["wb"])
    assert not offenders, f"path XLSX live/unsafe cells: {offenders!r}"
    assert len(summary_formulas) >= 3, summary_formulas
    blob = "\n".join(texts)
    expected = [_xlsx_form(payload(t, f)) for t in DANGEROUS for f in FIELDS]
    missing = [p for p in expected + [GAP_PAYLOAD_TITLE] if p not in blob]
    assert not missing, f"payload text dropped instead of neutralised: {missing!r}"


def test_path_xlsx_clean_cells_are_unchanged(written):
    wb = written["wb"]
    row = _mappings_row(wb, CONTROLS[6])
    assert {k: c.value for k, c in row.items()} == {
        "statement_id": "P7",
        "statement_text": CLEAN_STATEMENT,
        "framework_id": written["fw"].framework_id,
        "control_id": CONTROLS[6],
        "control_title": CLEAN_TITLE,
        "coverage": "full",
        "confidence": CLEAN_CONFIDENCE,
        "rationale": CLEAN_RATIONALE,
    }
    cs = wb["Crosswalk"]
    p7 = [r for r in cs.iter_rows(min_row=2) if r[0].value == "P7"]
    assert len(p7) == 1 and p7[0][1].value == CLEAN_STATEMENT
    gap = written["clean_gap"]
    gaps = {r[1].value: r[2].value for r in wb["Gaps"].iter_rows(min_row=2)}
    assert gaps[gap["id"]] == gap["title"]
