"""Data models for the control mapper.

These types define the contract between the policy input, the language model's
structured output, and the analysis layer. Keeping them in one place means the
JSON schema we hand to Claude and the objects we reason about never drift apart.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class CoverageLevel(str, Enum):
    """How completely a policy statement satisfies a control."""

    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class Control(BaseModel):
    """A single control drawn from a reference framework (e.g. NIST 800-53)."""

    id: str = Field(..., description="Control identifier, e.g. 'AC-2'.")
    family: str = Field(..., description="Control family, e.g. 'Access Control'.")
    title: str
    description: str


class Framework(BaseModel):
    """A reference framework: an identified, named set of controls."""

    framework_id: str
    framework_name: str
    controls: List[Control]
    source_note: Optional[str] = None

    def control_ids(self) -> set[str]:
        return {c.id for c in self.controls}

    def get(self, control_id: str) -> Optional[Control]:
        return next((c for c in self.controls if c.id == control_id), None)


class PolicyStatement(BaseModel):
    """One discrete requirement extracted from a policy document."""

    id: str = Field(..., description="Stable id for the statement, e.g. 'P1'.")
    text: str


class Mapping(BaseModel):
    """A single proposed link between a policy statement and a control.

    This is the unit the model produces. `confidence` and `rationale` exist so a
    human reviewer can audit *why* the link was proposed rather than trusting it
    blindly -- which matters in a GRC context where the output is evidence.
    """

    statement_id: str
    control_id: str
    coverage: CoverageLevel
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., description="Short justification for the mapping.")


class MappingResult(BaseModel):
    """The full set of mappings the model returned for one run."""

    framework_id: str
    mappings: List[Mapping] = Field(default_factory=list)


class GapAnalysis(BaseModel):
    """Derived view over a MappingResult, computed deterministically in code.

    None of this is produced by the model; it is calculated from the mappings so
    the numbers are reproducible and defensible.
    """

    framework_id: str
    covered_controls: List[str]
    partial_controls: List[str]
    uncovered_controls: List[str]
    unmapped_statements: List[str]
    coverage_ratio: float = Field(..., ge=0.0, le=1.0)
