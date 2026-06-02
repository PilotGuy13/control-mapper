"""Gap and over-coverage analysis.

Everything here is computed deterministically from a `MappingResult` -- no model
involved. That is deliberate: the headline numbers a hiring manager or auditor
cares about (what fraction of the framework is covered, which controls have no
evidence) must be reproducible and explainable, not a second opinion from an LLM.
"""

from __future__ import annotations

from typing import List

from .models import (
    CoverageLevel,
    Framework,
    GapAnalysis,
    MappingResult,
    PolicyStatement,
)


def analyze(
    result: MappingResult,
    framework: Framework,
    statements: List[PolicyStatement],
) -> GapAnalysis:
    """Derive coverage, gaps, and unmapped statements from raw mappings."""
    fully: set[str] = set()
    partially: set[str] = set()

    for m in result.mappings:
        if m.coverage == CoverageLevel.FULL:
            fully.add(m.control_id)
        elif m.coverage == CoverageLevel.PARTIAL:
            partially.add(m.control_id)

    # A control counted as "partial" only stays partial if nothing fully covers it.
    partial_only = partially - fully

    all_controls = framework.control_ids()
    covered = fully | partial_only
    uncovered = all_controls - covered

    mapped_statement_ids = {m.statement_id for m in result.mappings}
    # Statements that mapped to nothing: candidates for "out of scope" of this
    # framework, or a sign the framework subset is incomplete. Either way, a human
    # should look. This is the over-coverage / scope signal.
    unmapped_statements = [
        s.id for s in statements if s.id not in mapped_statement_ids
    ]

    coverage_ratio = len(covered) / len(all_controls) if all_controls else 0.0

    return GapAnalysis(
        framework_id=framework.framework_id,
        covered_controls=sorted(fully),
        partial_controls=sorted(partial_only),
        uncovered_controls=sorted(uncovered),
        unmapped_statements=sorted(unmapped_statements),
        coverage_ratio=round(coverage_ratio, 4),
    )


def render_report(
    result: MappingResult,
    analysis: GapAnalysis,
    framework: Framework,
    statements: List[PolicyStatement],
) -> str:
    """Produce a readable plain-text report suitable for a console or a file."""
    by_statement: dict[str, list] = {}
    for m in result.mappings:
        by_statement.setdefault(m.statement_id, []).append(m)

    statement_text = {s.id: s.text for s in statements}
    lines: List[str] = []
    lines.append(f"CONTROL MAPPING REPORT  |  {framework.framework_name}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(
        f"Framework coverage: {analysis.coverage_ratio:.0%} "
        f"({len(analysis.covered_controls) + len(analysis.partial_controls)}"
        f"/{len(framework.controls)} controls touched)"
    )
    lines.append("")

    lines.append("MAPPINGS BY POLICY STATEMENT")
    lines.append("-" * 72)
    for s in statements:
        text = statement_text[s.id]
        preview = text if len(text) <= 80 else text[:77] + "..."
        lines.append(f"{s.id}: {preview}")
        for m in by_statement.get(s.id, []):
            ctrl = framework.get(m.control_id)
            title = ctrl.title if ctrl else "?"
            lines.append(
                f"    -> {m.control_id} ({title}) "
                f"[{m.coverage.value}, conf {m.confidence:.2f}]"
            )
            lines.append(f"       {m.rationale}")
        if not by_statement.get(s.id):
            lines.append("    -> (no control mapped -- review for scope)")
        lines.append("")

    lines.append("UNCOVERED CONTROLS (gaps)")
    lines.append("-" * 72)
    if analysis.uncovered_controls:
        for cid in analysis.uncovered_controls:
            ctrl = framework.get(cid)
            lines.append(f"  {cid}: {ctrl.title if ctrl else ''}")
    else:
        lines.append("  None -- every control in scope has at least partial coverage.")
    lines.append("")

    lines.append("UNMAPPED STATEMENTS (possible out-of-scope / over-coverage)")
    lines.append("-" * 72)
    if analysis.unmapped_statements:
        for sid in analysis.unmapped_statements:
            lines.append(f"  {sid}: {statement_text.get(sid, '')}")
    else:
        lines.append("  None -- every statement mapped to at least one control.")

    return "\n".join(lines)
