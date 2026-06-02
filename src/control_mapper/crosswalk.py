"""Cross-framework crosswalk engine.

Every framework is mapped against the *same* set of policy statements. That shared
anchor is what makes a defensible crosswalk possible: if NIST AC-17 and ISO A.8.5
and SOC 2 CC6.6 all map to the same policy statement ("MFA for remote access"),
then for that obligation they are equivalent -- and the evidence for the claim is
the statement itself, not an analyst's assertion.

This module is pure derivation: given the per-framework mapping results, it builds
two views, both computed in code (no model involved):

  1. statement-anchored view -- for each policy statement, which controls in each
     framework satisfy it. This is the audit-friendly "one obligation, N framework
     citations" table.
  2. control-equivalence view -- for a chosen source framework, each of its mapped
     controls and the target-framework controls that share at least one statement
     with it, with the supporting statement ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .models import Framework, MappingResult, PolicyStatement


@dataclass
class CrosswalkInput:
    """One framework plus the mappings produced for it over the shared statements."""

    framework: Framework
    result: MappingResult


@dataclass
class StatementRow:
    statement_id: str
    statement_text: str
    # framework_id -> list of (control_id, coverage)
    by_framework: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)


@dataclass
class Crosswalk:
    framework_ids: List[str]
    rows: List[StatementRow]

    def to_dict(self) -> dict:
        return {
            "framework_ids": self.framework_ids,
            "rows": [
                {
                    "statement_id": r.statement_id,
                    "statement_text": r.statement_text,
                    "by_framework": {
                        fid: [{"control_id": c, "coverage": cov} for c, cov in pairs]
                        for fid, pairs in r.by_framework.items()
                    },
                }
                for r in self.rows
            ],
        }


def build_crosswalk(
    statements: List[PolicyStatement], inputs: List[CrosswalkInput]
) -> Crosswalk:
    """Build the statement-anchored crosswalk across all provided frameworks."""
    framework_ids = [i.framework.framework_id for i in inputs]

    # statement_id -> framework_id -> [(control_id, coverage)]
    index: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
        s.id: {fid: [] for fid in framework_ids} for s in statements
    }
    for inp in inputs:
        fid = inp.framework.framework_id
        for m in inp.result.mappings:
            if m.statement_id in index:
                index[m.statement_id][fid].append((m.control_id, m.coverage.value))

    rows = [
        StatementRow(
            statement_id=s.id,
            statement_text=s.text,
            by_framework={
                fid: sorted(index[s.id][fid]) for fid in framework_ids
            },
        )
        for s in statements
    ]
    return Crosswalk(framework_ids=framework_ids, rows=rows)


def equivalences_for(
    crosswalk: Crosswalk, source_framework_id: str
) -> Dict[str, Dict[str, Dict[str, List[str]]]]:
    """Control-equivalence view anchored on one source framework.

    Returns: source_control -> target_framework -> target_control -> [statement_ids]
    i.e. "this source control lines up with these target controls, and here is the
    statement evidence for each correspondence."
    """
    out: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
    target_ids = [f for f in crosswalk.framework_ids if f != source_framework_id]

    for row in crosswalk.rows:
        source_controls = [c for c, _ in row.by_framework.get(source_framework_id, [])]
        if not source_controls:
            continue
        for sc in source_controls:
            bucket = out.setdefault(sc, {tid: {} for tid in target_ids})
            for tid in target_ids:
                for tc, _cov in row.by_framework.get(tid, []):
                    bucket[tid].setdefault(tc, []).append(row.statement_id)
    return out


def render_crosswalk(crosswalk: Crosswalk, statements: List[PolicyStatement]) -> str:
    """Readable statement-anchored crosswalk report."""
    lines: List[str] = []
    lines.append("CROSS-FRAMEWORK CROSSWALK (anchored on policy statements)")
    lines.append("=" * 72)
    lines.append("Frameworks: " + ", ".join(crosswalk.framework_ids))
    lines.append("")

    for row in crosswalk.rows:
        preview = (
            row.statement_text
            if len(row.statement_text) <= 78
            else row.statement_text[:75] + "..."
        )
        lines.append(f"{row.statement_id}: {preview}")
        for fid in crosswalk.framework_ids:
            pairs = row.by_framework.get(fid, [])
            if pairs:
                rendered = ", ".join(f"{c} [{cov}]" for c, cov in pairs)
            else:
                rendered = "(no control -- gap or out of scope)"
            lines.append(f"    {fid:24s} {rendered}")
        lines.append("")
    return "\n".join(lines)


def render_equivalences(
    equivs: Dict[str, Dict[str, Dict[str, List[str]]]], source_framework_id: str
) -> str:
    """Readable control-equivalence report for one source framework."""
    lines: List[str] = []
    lines.append(f"CONTROL EQUIVALENCES (source: {source_framework_id})")
    lines.append("=" * 72)
    for sc in sorted(equivs):
        lines.append(f"{sc}")
        for tid, controls in equivs[sc].items():
            if controls:
                rendered = ", ".join(
                    f"{tc} (via {','.join(stmts)})"
                    for tc, stmts in sorted(controls.items())
                )
            else:
                rendered = "(no equivalent found)"
            lines.append(f"    -> {tid:24s} {rendered}")
        lines.append("")
    return "\n".join(lines)
