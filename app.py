"""Streamlit UI -- a face for the control mapper.

Run with:
    streamlit run app.py

Paste or upload a policy, pick a framework, and view mappings, coverage, gaps, and
unmapped statements. Use the sidebar toggle to run offline (stub client) for a
zero-cost demo, or against the live model with ANTHROPIC_API_KEY set.

This file is intentionally thin: all logic lives in the `control_mapper` package
and is unit-tested. The UI only orchestrates and displays.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from control_mapper import (  # noqa: E402
    ControlMapper,
    analyze,
    load_framework,
    parse_policy,
)

FRAMEWORK_FILES = {
    "NIST SP 800-53 Rev. 5": ROOT / "data" / "frameworks" / "nist_800-53_subset.json",
    "ISO/IEC 27001:2022 Annex A": ROOT / "data" / "frameworks" / "iso_27001_2022_annexa_subset.json",
    "SOC 2 Trust Services Criteria": ROOT / "data" / "frameworks" / "soc2_tsc_subset.json",
}
SAMPLE_PATH = ROOT / "data" / "sample_policies" / "sample_policy.md"

st.set_page_config(page_title="Control Mapper", layout="wide")
st.title("Policy → Control Mapper")
st.caption(
    "Maps organizational policy statements to compliance framework controls, "
    "then flags coverage gaps and out-of-scope statements for human review."
)

with st.sidebar:
    st.header("Settings")
    framework_label = st.selectbox("Framework", list(FRAMEWORK_FILES.keys()))
    offline = st.toggle("Offline demo (no API key)", value=True)
    st.markdown(
        "Offline mode uses a deterministic stub so you can demo without an API "
        "key or network. Turn it off to run against Claude (needs "
        "`ANTHROPIC_API_KEY`)."
    )
    st.divider()
    st.markdown(
        "**Reminder:** mappings are AI-assisted drafts. A qualified human should "
        "review every mapping before it is used as audit evidence."
    )


def get_client(offline: bool):
    if offline:
        from eval.stub_client import StubClient

        return StubClient()
    from control_mapper import AnthropicClient

    return AnthropicClient()


framework = load_framework(FRAMEWORK_FILES[framework_label])

default_text = SAMPLE_PATH.read_text(encoding="utf-8")
policy_text = st.text_area("Policy text (numbered statements)", default_text, height=280)

if st.button("Map controls", type="primary"):
    statements = parse_policy(policy_text)
    if not statements:
        st.error("No numbered statements found. Number your lines '1.', '2.', ...")
        st.stop()

    try:
        client = get_client(offline)
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    with st.spinner("Mapping..."):
        result = ControlMapper(client).map(statements, framework)
        analysis = analyze(result, framework, statements)

    touched = len(analysis.covered_controls) + len(analysis.partial_controls)
    c1, c2, c3 = st.columns(3)
    c1.metric("Framework coverage", f"{analysis.coverage_ratio:.0%}")
    c2.metric("Controls touched", f"{touched}/{len(framework.controls)}")
    c3.metric("Unmapped statements", len(analysis.unmapped_statements))

    st.subheader("Mappings")
    rows = []
    for m in result.mappings:
        ctrl = framework.get(m.control_id)
        rows.append(
            {
                "Statement": m.statement_id,
                "Control": m.control_id,
                "Title": ctrl.title if ctrl else "",
                "Coverage": m.coverage.value,
                "Confidence": round(m.confidence, 2),
                "Rationale": m.rationale,
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Uncovered controls (gaps)")
        if analysis.uncovered_controls:
            for cid in analysis.uncovered_controls:
                ctrl = framework.get(cid)
                st.write(f"- **{cid}** {ctrl.title if ctrl else ''}")
        else:
            st.write("None in scope.")
    with col_right:
        st.subheader("Unmapped statements")
        if analysis.unmapped_statements:
            text_by_id = {s.id: s.text for s in statements}
            for sid in analysis.unmapped_statements:
                st.write(f"- **{sid}**: {text_by_id[sid]}")
        else:
            st.write("Every statement mapped to a control.")
