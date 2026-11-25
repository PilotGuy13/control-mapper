"""Export crosswalk results to CSV and formatted XLSX.

Two outputs, both aimed at a GRC reader who lives in Excel:

- CSV  : a flat, long-format table (one row per mapping) that drops straight into
         any GRC tool, BI tool, or pivot table.
- XLSX : a formatted workbook with four sheets -- Summary, Crosswalk (wide,
         one row per obligation), Mappings (long, auditable detail with rationale),
         and Gaps (controls no statement covered, per framework).

Design notes for reviewers:
- Detail (control title, coverage, confidence, rationale) comes from the original
  mappings, not just the crosswalk rows, so the export is fully auditable.
- The Summary sheet's headline counts are written as Excel formulas (COUNTIF /
  COUNTA over the Mappings sheet) rather than hardcoded, so the workbook stays
  live if a reviewer edits the detail. Run the xlsx skill's recalc to populate
  cached values.
- Every text cell goes through a formula-injection guard: a value starting with
  = + - @ TAB or CR gets a leading single quote, so no spreadsheet runs it.
- `crosswalk_csv_bytes` / `crosswalk_xlsx_bytes` build the same files in memory
  (no temp files) for the UI. With `review=state` they build the ACCEPTED export:
  accepted mappings only, plus decision, AI coverage, reason, actor and time.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from .crosswalk import Crosswalk, CrosswalkInput

if TYPE_CHECKING:  # pragma: no cover
    from .review import ReviewState

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _safe(value):
    """Neutralise spreadsheet formula triggers in text cells (non-text unchanged)."""
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGERS):
        return "'" + value
    return value


@dataclass
class _Record:
    statement_id: str
    statement_text: str
    framework_id: str
    control_id: str
    control_title: str
    coverage: str
    confidence: float
    rationale: str
    # Accepted export only (review=state).
    policy_name: str = ""
    decision: str = ""
    ai_coverage: str = ""
    reason: str = ""
    coverage_reason: str = ""
    source_url: str = ""
    reviewed_at: str = ""
    actor: str = ""

    def row(self, header: List[str]) -> list:
        return [_safe(getattr(self, name)) for name in header]


def _build_records(
    statements, inputs: List[CrosswalkInput], review: Optional[ReviewState] = None
) -> List[_Record]:
    """Flatten every framework's mappings into one long-format list of records."""
    from .review import ReviewStatus, key_for

    text_by_id = {s.id: s.text for s in statements}
    records: List[_Record] = []
    for inp in inputs:
        fw = inp.framework
        for m in inp.result.mappings:
            ctrl = fw.get(m.control_id)
            rec = _Record(
                statement_id=m.statement_id,
                statement_text=text_by_id.get(m.statement_id, ""),
                framework_id=fw.framework_id,
                control_id=m.control_id,
                control_title=ctrl.title if ctrl else "",
                coverage=m.coverage.value,
                confidence=round(m.confidence, 2),
                rationale=m.rationale,
            )
            if review is not None:
                try:
                    key = key_for(review, fw.framework_id, m.statement_id, m.control_id)
                except KeyError:
                    msg = "accepted export: mapping not in the review"
                    raise ValueError(msg) from None
                item = review.items[key]
                if item.status != ReviewStatus.ACCEPTED:
                    # Raw inputs passed by mistake: never export pending/rejected rows.
                    raise ValueError(
                        "accepted export: pass accepted_inputs(state, raw), not raw inputs"
                    )
                rec.policy_name = review.policy_id
                rec.decision = item.status.value
                rec.coverage = item.coverage.value
                rec.ai_coverage = item.mapping.coverage.value
                rec.reason = item.reason or ""
                rec.coverage_reason = item.coverage_reason or ""
                rec.actor = item.actor or ""
                rec.reviewed_at = item.at.isoformat() if item.at is not None else ""
                # Links come from the framework JSON only, never from model output.
                ctrl_url = ctrl.source_url if ctrl else None
                rec.source_url = ctrl_url or fw.source_url or ""
            records.append(rec)
    # Stable, human-friendly order: statement, then framework, then control.
    records.sort(key=lambda r: (_statement_sort_key(r.statement_id), r.framework_id, r.control_id))
    return records


def _statement_sort_key(statement_id: str):
    # "P10" should sort after "P9", so sort by the trailing integer when present.
    digits = "".join(ch for ch in statement_id if ch.isdigit())
    return (int(digits) if digits else 0, statement_id)


CSV_HEADER = [
    "statement_id",
    "statement_text",
    "framework_id",
    "control_id",
    "control_title",
    "coverage",
    "confidence",
    "rationale",
]


ACCEPTED_CSV_HEADER = [
    "policy_name",
    "statement_id",
    "statement_text",
    "framework_id",
    "control_id",
    "control_title",
    "decision",
    "coverage",
    "ai_coverage",
    "confidence",
    "reason",
    "coverage_reason",
    "rationale",
    "source_url",
    "reviewed_at",
    "actor",
]


def _csv_text(records: List[_Record], header: List[str]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in records:
        writer.writerow(r.row(header))
    return buf.getvalue()


def crosswalk_to_csv(
    crosswalk: Crosswalk, statements, inputs: List[CrosswalkInput], path: str | Path
) -> int:
    """Write the long-format crosswalk to CSV. Returns the number of rows written."""
    records = _build_records(statements, inputs)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(_csv_text(records, CSV_HEADER))
    return len(records)


def crosswalk_csv_bytes(
    crosswalk: Crosswalk,
    statements,
    inputs: List[CrosswalkInput],
    review: Optional[ReviewState] = None,
) -> bytes:
    """CSV in memory. review=None: the draft, identical to `crosswalk_to_csv`.

    review=state: the accepted export. Pass inputs = accepted_inputs(state, raw).
    """
    header = CSV_HEADER if review is None else ACCEPTED_CSV_HEADER
    return _csv_text(_build_records(statements, inputs, review), header).encode("utf-8")


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

_FONT_NAME = "Arial"

_COL_WIDTHS = {
    "policy_name": 30,
    "statement_id": 12,
    "statement_text": 48,
    "framework_id": 20,
    "control_id": 12,
    "control_title": 30,
    "decision": 11,
    "coverage": 11,
    "ai_coverage": 11,
    "confidence": 11,
    "reason": 40,
    "coverage_reason": 40,
    "rationale": 52,
    "source_url": 40,
    "reviewed_at": 26,
    "actor": 16,
}
_WRAP_COLS = {"statement_text", "rationale", "reason", "coverage_reason"}

_NO_CONTROL_DRAFT = "(gap / out of scope)"
_NO_CONTROL_ACCEPTED = "No control mapped"


def _pending_by_cell(review: ReviewState) -> Dict[tuple, List[str]]:
    """(statement_label, framework_id) -> sorted control ids still PENDING."""
    from .review import ReviewStatus

    out: Dict[tuple, List[str]] = {}
    for item in review.items.values():
        if item.status == ReviewStatus.PENDING:
            cell = (item.mapping.statement_id, item.framework_id)
            out.setdefault(cell, []).append(item.mapping.control_id)
    return {k: sorted(v) for k, v in out.items()}


def _build_workbook(
    crosswalk: Crosswalk,
    statements,
    inputs: List[CrosswalkInput],
    review: Optional[ReviewState] = None,
):
    """Build the formatted four-sheet workbook (draft, or accepted when review given)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from .review import ReviewStatus

    header_font = Font(name=_FONT_NAME, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    base_font = Font(name=_FONT_NAME)
    title_font = Font(name=_FONT_NAME, bold=True, size=14)
    wrap = Alignment(vertical="top", wrap_text=True)
    top = Alignment(vertical="top")
    full_fill = PatternFill("solid", fgColor="E2EFDA")     # soft green
    partial_fill = PatternFill("solid", fgColor="FFF2CC")  # soft amber
    gap_fill = PatternFill("solid", fgColor="FCE4E4")      # soft red

    records = _build_records(statements, inputs, review)
    framework_ids = crosswalk.framework_ids
    header = CSV_HEADER if review is None else ACCEPTED_CSV_HEADER
    pending = _pending_by_cell(review) if review is not None else {}

    wb = Workbook()

    def style_header(ws, ncols: int) -> None:
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)

    # --- Mappings sheet (long, auditable) -- built first so Summary can reference it.
    ms = wb.active
    ms.title = "Mappings"
    ms.append(header)
    for r in records:
        ms.append(r.row(header))
    style_header(ms, len(header))
    wrap_idx = {header.index(n) + 1 for n in _WRAP_COLS if n in header}
    cov_idx = header.index("coverage")
    for row in ms.iter_rows(min_row=2):
        for cell in row:
            cell.font = base_font
            cell.alignment = wrap if cell.column in wrap_idx else top
        cov_cell = row[cov_idx]
        cov_cell.fill = full_fill if cov_cell.value == "full" else partial_fill
    for i, name in enumerate(header, start=1):
        ms.column_dimensions[get_column_letter(i)].width = _COL_WIDTHS.get(name, 14)
    ms.freeze_panes = "A2"
    ms.auto_filter.ref = f"A1:{get_column_letter(len(header))}{ms.max_row}"
    last = ms.max_row  # last data row in Mappings; used by Summary formulas
    fw_col = get_column_letter(header.index("framework_id") + 1)
    cov_col = get_column_letter(cov_idx + 1)

    # --- Crosswalk sheet (wide, one row per obligation) ---
    cs = wb.create_sheet("Crosswalk")
    cs_header = ["Statement ID", "Statement"] + framework_ids
    cs.append(cs_header)
    text_by_id = {s.id: s.text for s in statements}
    pending_cells = set()
    for row in crosswalk.rows:
        cells = [_safe(row.statement_id), _safe(text_by_id.get(row.statement_id, ""))]
        for fid in framework_ids:
            pairs = row.by_framework.get(fid, [])
            accepted = "; ".join(f"{c} [{cov}]" for c, cov in pairs)
            if review is None:
                cells.append(accepted if pairs else _NO_CONTROL_DRAFT)
                continue
            waiting = pending.get((row.statement_id, fid), [])
            if pairs and waiting:
                cells.append(f"{accepted}; + pending: {', '.join(waiting)}")
            elif pairs:
                cells.append(accepted)
            elif waiting:
                cells.append(f"Pending review: {', '.join(waiting)}")
                pending_cells.add((cs.max_row + 1, len(cells) + 1))
            else:
                cells.append(_NO_CONTROL_ACCEPTED)
        cs.append([_safe(c) for c in cells])
    style_header(cs, len(cs_header))
    for row in cs.iter_rows(min_row=2):
        for cell in row:
            cell.font = base_font
            cell.alignment = wrap
        for cell in row[2:]:
            if cell.value in (_NO_CONTROL_DRAFT, _NO_CONTROL_ACCEPTED):
                cell.fill = gap_fill
            elif (cell.row, cell.column) in pending_cells:
                cell.fill = partial_fill
    cs.column_dimensions["A"].width = 12
    cs.column_dimensions["B"].width = 50
    for i in range(len(framework_ids)):
        cs.column_dimensions[get_column_letter(3 + i)].width = 26
    cs.freeze_panes = "C2"

    # --- Gaps sheet (uncovered controls per framework) ---
    gs = wb.create_sheet("Gaps")
    gs.append(["Framework", "Control ID", "Control Title", "Status"])
    pending_controls = set()
    if review is not None:
        pending_controls = {
            (it.framework_id, it.mapping.control_id)
            for it in review.items.values()
            if it.status == ReviewStatus.PENDING
        }
    for inp in inputs:
        fw = inp.framework
        touched = {m.control_id for m in inp.result.mappings}
        for ctrl in fw.controls:
            if ctrl.id not in touched:
                status = (
                    "pending review"
                    if (fw.framework_id, ctrl.id) in pending_controls
                    else "uncovered"
                )
                gs.append(
                    [_safe(fw.framework_id), _safe(ctrl.id), _safe(ctrl.title), status]
                )
    style_header(gs, 4)
    for row in gs.iter_rows(min_row=2):
        for cell in row:
            cell.font = base_font
            cell.alignment = top
            cell.fill = gap_fill
    gs.column_dimensions["A"].width = 22
    gs.column_dimensions["B"].width = 12
    gs.column_dimensions["C"].width = 40
    gs.column_dimensions["D"].width = 12
    gs.freeze_panes = "A2"

    # --- Summary sheet (counts via formulas over Mappings) ---
    summ = wb.create_sheet("Summary", 0)  # first sheet
    summ["A1"] = "Policy-to-Control Crosswalk Summary"
    summ["A1"].font = title_font
    if review is None:
        summ["A3"] = (
            "AI-assisted draft. A qualified human must review every mapping before use "
            "as audit evidence. Counts below are live Excel formulas over the Mappings sheet."
        )
    else:
        statuses = [it.status for it in review.items.values()]
        n_accepted = statuses.count(ReviewStatus.ACCEPTED)
        n_pending = statuses.count(ReviewStatus.PENDING)
        summ["A2"] = _safe(f"Policy: {review.policy_id}")
        summ["A2"].font = base_font
        summ["A3"] = (
            f"Reviewed: accepted mappings only ({n_accepted} of {len(statuses)}). "
            "Pending and rejected mappings are excluded."
        )
        summ["A4"] = f"Pending mappings: {n_pending} (not counted as coverage)"
        summ["A4"].font = base_font
    summ["A3"].font = Font(name=_FONT_NAME, italic=True)
    summ["A3"].alignment = wrap
    summ.merge_cells("A3:E3")

    head_row = 5
    headers = ["Framework", "Controls in framework", "Mappings", "Full", "Partial"]
    for c, h in enumerate(headers, start=1):
        cell = summ.cell(row=head_row, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    fw_range = f"Mappings!${fw_col}$2:${fw_col}${last}"
    cov_range = f"Mappings!${cov_col}$2:${cov_col}${last}"
    size_by_fw = {inp.framework.framework_id: len(inp.framework.controls) for inp in inputs}
    for i, fid in enumerate(framework_ids):
        row_no = head_row + 1 + i
        summ.cell(row=row_no, column=1, value=_safe(fid)).font = base_font
        summ.cell(row=row_no, column=2, value=size_by_fw.get(fid, 0)).font = base_font
        # Mappings (total rows for this framework) via COUNTIF on the framework column
        summ.cell(row=row_no, column=3,
                  value=f'=COUNTIF({fw_range},A{row_no})').font = base_font
        # Full / Partial via COUNTIFS on framework + coverage
        summ.cell(row=row_no, column=4,
                  value=f'=COUNTIFS({fw_range},A{row_no},{cov_range},"full")').font = base_font
        summ.cell(row=row_no, column=5,
                  value=f'=COUNTIFS({fw_range},A{row_no},{cov_range},"partial")').font = base_font
    summ.column_dimensions["A"].width = 24
    for col in ("B", "C", "D", "E"):
        summ.column_dimensions[col].width = 20

    return wb


# Kept on purpose: wb.save() spools worksheets through temp files (forbidden by the
# no-disk tests); this relies on openpyxl internals, hence the <3.2 pin.
def _workbook_bytes(wb) -> bytes:
    """Serialise a workbook entirely in memory.

    openpyxl's own `save()` streams each worksheet through a temporary file on disk,
    even when the target is a BytesIO. This writer keeps every worksheet in a BytesIO
    instead. It relies on openpyxl's writer internals (checked on openpyxl 3.1.5).
    """
    import datetime
    from zipfile import ZIP_DEFLATED, ZipFile

    from openpyxl.drawing.spreadsheet_drawing import SpreadsheetDrawing
    from openpyxl.worksheet._writer import WorksheetWriter
    from openpyxl.writer.excel import ExcelWriter

    class _InMemoryExcelWriter(ExcelWriter):
        def write_worksheet(self, ws):
            ws._drawing = SpreadsheetDrawing()
            ws._drawing.charts = ws._charts
            ws._drawing.images = ws._images
            writer = WorksheetWriter(ws, out=io.BytesIO())
            writer.write()
            ws._rels = writer._rels
            self._archive.writestr(ws.path[1:], writer.read())
            self.manifest.append(ws)

    buf = io.BytesIO()
    archive = ZipFile(buf, "w", ZIP_DEFLATED, allowZip64=True)
    wb.properties.modified = datetime.datetime.now(tz=datetime.UTC).replace(
        tzinfo=None
    )
    _InMemoryExcelWriter(wb, archive).save()
    return buf.getvalue()


def crosswalk_to_xlsx(
    crosswalk: Crosswalk, statements, inputs: List[CrosswalkInput], path: str | Path
) -> None:
    """Write a formatted, multi-sheet workbook. Requires openpyxl."""
    data = _workbook_bytes(_build_workbook(crosswalk, statements, inputs))
    with open(path, "wb") as fh:
        fh.write(data)


def crosswalk_xlsx_bytes(
    crosswalk: Crosswalk,
    statements,
    inputs: List[CrosswalkInput],
    review: Optional[ReviewState] = None,
) -> bytes:
    """XLSX in memory. review=None: the draft, cell-identical to `crosswalk_to_xlsx`.

    review=state: the accepted export. Pass inputs = accepted_inputs(state, raw) and
    crosswalk = build_crosswalk(statements, those inputs).
    """
    return _workbook_bytes(_build_workbook(crosswalk, statements, inputs, review))
