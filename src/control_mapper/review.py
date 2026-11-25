"""Human review of AI-proposed mappings (pure logic, no Streamlit, no I/O).

A reviewer accepts, rejects or corrects the coverage of each mapping the model
proposed. Decisions are keyed by a stable identity (ADR 0001 D2/D11), never by the
positional label "P3": the key is (policy_id, statement_hash, occurrence,
framework_id, control_id). Renumbering a policy therefore keeps every decision,
while editing a statement's text produces a new hash and starts that statement's
mappings at pending again.

The reason rule (ADR 0001 D4, owner-confirmed): rejecting a mapping or changing its
coverage requires a non-blank reason; accepting does not. Nothing here calls the
clock -- the caller passes `at` -- and there is deliberately no bulk-accept path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .crosswalk import CrosswalkInput
from .models import CoverageLevel, Mapping, PolicyStatement


class ReviewStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


# (policy_id, statement_hash, occurrence)
StatementKey = Tuple[str, str, int]
# (policy_id, statement_hash, occurrence, framework_id, control_id)
MappingKey = Tuple[str, str, int, str, str]


def normalise_statement(text: str) -> str:
    """Collapse all whitespace runs to one space and trim (whitespace only)."""
    return " ".join(text.split())


def statement_hash(text: str) -> str:
    """SHA-256 hex digest of the whitespace-normalised statement text."""
    return hashlib.sha256(normalise_statement(text).encode("utf-8")).hexdigest()


def statement_keys(
    policy_id: str, statements: List[PolicyStatement]
) -> Dict[str, StatementKey]:
    """Display label ("P3") -> stable statement key for this run.

    `occurrence` is the 1-based ordinal among statements with identical normalised
    text, so duplicate statements never collide.
    """
    seen: Dict[str, int] = {}
    out: Dict[str, StatementKey] = {}
    for s in statements:
        if s.id in out:
            # e.g. numbering restarts (P1, P2, P1): labels would be ambiguous.
            raise ValueError(f"duplicate statement number: {s.id}")
        h = statement_hash(s.text)
        seen[h] = seen.get(h, 0) + 1
        out[s.id] = (policy_id, h, seen[h])
    return out


@dataclass
class ReviewItem:
    framework_id: str
    mapping: Mapping  # the original model proposal, never mutated
    coverage: CoverageLevel  # the reviewer's coverage; starts as mapping.coverage
    status: ReviewStatus = ReviewStatus.PENDING
    actor: Optional[str] = None
    reason: Optional[str] = None
    at: Optional[datetime] = None
    # Why the coverage was changed. Written only by set_coverage; set_status never
    # clears it, so a later accept or reject keeps the coverage justification.
    coverage_reason: Optional[str] = None


@dataclass
class ReviewState:
    policy_id: str
    items: Dict[MappingKey, ReviewItem]
    labels: Dict[str, StatementKey] = field(default_factory=dict)


def _has_reason(reason: Optional[str]) -> bool:
    return reason is not None and reason.strip() != ""


def start_review(
    policy_id: str,
    statements: List[PolicyStatement],
    inputs: List[CrosswalkInput],
    previous: Optional[ReviewState] = None,
) -> ReviewState:
    """One PENDING item per mapping; decisions in `previous` carry over by stable key."""
    labels = statement_keys(policy_id, statements)
    items: Dict[MappingKey, ReviewItem] = {}
    for inp in inputs:
        fid = inp.framework.framework_id
        for m in inp.result.mappings:
            if m.statement_id not in labels:
                raise ValueError("mapping references a statement not in this policy")
            key: MappingKey = (*labels[m.statement_id], fid, m.control_id)
            if key in items:
                continue  # duplicate proposal of the same link; review it once
            item = ReviewItem(framework_id=fid, mapping=m, coverage=m.coverage)
            prev = previous.items.get(key) if previous is not None else None
            if prev is not None:
                item.status = prev.status
                item.coverage = prev.coverage
                item.actor = prev.actor
                item.reason = prev.reason
                item.at = prev.at
                item.coverage_reason = prev.coverage_reason
            items[key] = item
    return ReviewState(policy_id=policy_id, items=items, labels=labels)


def key_for(
    state: ReviewState, framework_id: str, statement_label: str, control_id: str
) -> MappingKey:
    """Label of THIS run -> stable key. KeyError if no such mapping exists."""
    key: MappingKey = (*state.labels[statement_label], framework_id, control_id)
    if key not in state.items:
        raise KeyError(key)
    return key


def set_status(
    state: ReviewState,
    key: MappingKey,
    status: ReviewStatus,
    *,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    at: Optional[datetime] = None,
) -> None:
    """Set a decision. REJECTED needs a non-blank reason; state unchanged on error."""
    item = state.items[key]
    status = ReviewStatus(status)
    if status == ReviewStatus.REJECTED and not _has_reason(reason):
        raise ValueError("a reason is required to reject a mapping")
    item.status = status
    item.actor = actor
    item.reason = reason if _has_reason(reason) else None
    item.at = at


def set_coverage(
    state: ReviewState,
    key: MappingKey,
    coverage: CoverageLevel,
    *,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    at: Optional[datetime] = None,
) -> None:
    """Set the reviewer's coverage (FULL/PARTIAL). A change needs a non-blank reason."""
    item = state.items[key]
    coverage = CoverageLevel(coverage)
    if coverage == CoverageLevel.NONE:
        raise ValueError("coverage 'none' is not allowed; reject the mapping instead")
    if coverage == item.coverage:
        return  # same coverage again: a no-op, with or without a reason
    if not _has_reason(reason):
        raise ValueError("a reason is required to change coverage")
    # The decision reason (item.reason) belongs to set_status and is never touched here.
    item.coverage = coverage
    item.coverage_reason = reason.strip()
    item.actor = actor
    item.at = at


def accepted_inputs(
    state: ReviewState, inputs: List[CrosswalkInput]
) -> List[CrosswalkInput]:
    """Same frameworks and order; only ACCEPTED mappings, with the reviewer's coverage.

    Returns new objects; `inputs` is not mutated.
    """
    out: List[CrosswalkInput] = []
    for inp in inputs:
        fid = inp.framework.framework_id
        kept: List[Mapping] = []
        seen = set()
        for m in inp.result.mappings:
            skey = state.labels.get(m.statement_id)
            if skey is None:
                continue
            key = (*skey, fid, m.control_id)
            item = state.items.get(key)
            if item is None or item.status != ReviewStatus.ACCEPTED or key in seen:
                continue
            seen.add(key)
            kept.append(m.model_copy(update={"coverage": item.coverage}))
        out.append(
            CrosswalkInput(
                inp.framework, inp.result.model_copy(update={"mappings": kept})
            )
        )
    return out
