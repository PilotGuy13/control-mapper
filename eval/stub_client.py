"""Deterministic, framework-aware stub client.

Stands in for the live Anthropic client so the project runs end-to-end with no API
key, no network, and no cost -- for the `--offline` demo, the unit tests, and
offline runs of the evaluation harness and the crosswalk.

For each supported framework it returns a *fixed, deliberately imperfect* set of
mappings for the bundled sample policy. The imperfections are what let the
evaluation harness demonstrate real precision/recall below 1.0 with concrete false
positives and false negatives -- a stub that echoed the gold standard would make
the eval look perfect and prove nothing.

Which framework is being mapped is detected from the prompt text (the mapper writes
"(<framework_id>)" into the user content). This keeps the simple `MapperClient`
protocol intact -- the stub sees exactly what the real client sees.
"""

from __future__ import annotations

import re
from typing import List

# (statement_id, control_id, coverage, confidence, rationale)
# Each set encodes intentional, documented errors vs the gold standard.

_NIST = [
    # Intentional: MISSES P2->AC-3, MISSES P7->IR-4, ADDS P5->SC-7 (false positive).
    ("P1", "AT-2", "full", 0.95, "Requires awareness training on hire and annually."),
    ("P2", "AC-6", "full", 0.93, "States access is granted on a least-privilege basis."),
    ("P2", "AC-2", "full", 0.88, "Requires owner approval and quarterly access review."),
    ("P3", "IA-2", "full", 0.90, "Requires MFA to authenticate users for access."),
    ("P3", "AC-17", "full", 0.92, "Requires MFA specifically for remote access."),
    ("P4", "IA-5", "full", 0.94, "Sets password length, rotation, and hashed storage."),
    ("P5", "SC-28", "full", 0.93, "Requires customer data encrypted at rest."),
    ("P5", "SC-13", "full", 0.85, "Specifies approved cryptography (AES-256, TLS)."),
    ("P5", "SC-7", "partial", 0.40, "Mentions the network, loosely related to boundaries."),
    ("P6", "AU-6", "full", 0.90, "Requires weekly review of logs for anomalies."),
    ("P6", "AU-2", "partial", 0.70, "Implies which events are logged centrally."),
    ("P7", "IR-6", "full", 0.92, "Requires incident reporting within one hour."),
    ("P8", "RA-5", "full", 0.91, "Requires monthly vulnerability scanning."),
    ("P8", "SI-2", "partial", 0.75, "Requires remediation of critical findings."),
    ("P10", "CP-9", "full", 0.93, "Requires daily encrypted backups."),
    ("P10", "CP-10", "partial", 0.72, "Requires quarterly restorability testing."),
]

_ISO = [
    # Intentional: MISSES P2->A.5.18, MISSES P10->A.5.30, ADDS P5->A.8.20 (false positive).
    ("P1", "A.6.3", "full", 0.95, "Requires security awareness training on hire and annually."),
    ("P2", "A.5.15", "full", 0.92, "Defines least-privilege access control rules."),
    ("P3", "A.8.5", "full", 0.91, "Requires MFA, a secure authentication mechanism."),
    ("P3", "A.5.17", "partial", 0.68, "Touches handling of authentication information."),
    ("P4", "A.5.17", "full", 0.93, "Governs passwords as authentication information."),
    ("P5", "A.8.24", "full", 0.92, "Specifies cryptography for data at rest and in transit."),
    ("P5", "A.8.20", "partial", 0.38, "Mentions the corporate network in passing."),
    ("P6", "A.8.15", "full", 0.90, "Requires central log collection and retention."),
    ("P6", "A.8.16", "partial", 0.70, "Requires weekly review for anomalous activity."),
    ("P7", "A.5.24", "full", 0.88, "References a documented incident response runbook."),
    ("P7", "A.5.26", "full", 0.90, "Requires response to suspected incidents."),
    ("P8", "A.8.8", "full", 0.91, "Requires vulnerability scanning and remediation."),
    ("P10", "A.8.13", "full", 0.93, "Requires daily, tested information backups."),
]

_SOC2 = [
    # Intentional: MISSES P1->CC2.2, MISSES P9->CC9.1 (model fails to see insurance as
    # risk mitigation -- a realistic and instructive miss), ADDS P6->CC4.1 (false positive).
    ("P1", "CC1.4", "full", 0.90, "Develops competent personnel via required training."),
    ("P2", "CC6.1", "full", 0.92, "Implements least-privilege logical access measures."),
    ("P2", "CC6.2", "full", 0.88, "Requires owner authorization before granting access."),
    ("P2", "CC6.3", "partial", 0.74, "Requires quarterly review of access rights."),
    ("P3", "CC6.1", "full", 0.90, "Requires MFA as a logical access measure."),
    ("P3", "CC6.6", "full", 0.89, "Protects remote access from outside the boundary."),
    ("P4", "CC6.1", "full", 0.91, "Sets credential strength and storage requirements."),
    ("P5", "CC6.7", "full", 0.92, "Protects data in transit via TLS."),
    ("P5", "C1.2", "partial", 0.70, "Protects confidential customer data at rest."),
    ("P6", "CC7.2", "full", 0.90, "Monitors logs for anomalous activity."),
    ("P6", "CC4.1", "partial", 0.42, "Loosely resembles monitoring of controls."),
    ("P7", "CC7.3", "full", 0.88, "Evaluates and reports suspected incidents."),
    ("P7", "CC7.4", "full", 0.90, "Executes a documented incident response runbook."),
    ("P8", "CC7.1", "full", 0.91, "Detects vulnerabilities via monthly scanning."),
    ("P10", "A1.2", "full", 0.92, "Maintains tested backup and recovery processes."),
]

_BY_FRAMEWORK = {
    "NIST_SP_800-53_R5": _NIST,
    "ISO_IEC_27001_2022": _ISO,
    "SOC2_TSC_2017": _SOC2,
}


def _detect_framework_id(user_content: str) -> str | None:
    """The mapper writes 'FRAMEWORK: <name> (<framework_id>)' into the prompt."""
    for fid in _BY_FRAMEWORK:
        if fid in user_content:
            return fid
    match = re.search(r"\(([^)]+)\)", user_content)
    return match.group(1) if match else None


class StubClient:
    """Implements the MapperClient protocol with fixed, framework-aware output."""

    def get_mappings(self, system: str, tool: dict, user_content: str) -> List[dict]:
        fid = _detect_framework_id(user_content)
        rows = _BY_FRAMEWORK.get(fid, [])
        return [
            {
                "statement_id": sid,
                "control_id": cid,
                "coverage": cov,
                "confidence": conf,
                "rationale": rat,
            }
            for (sid, cid, cov, conf, rat) in rows
        ]
