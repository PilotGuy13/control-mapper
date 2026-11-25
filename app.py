"""Streamlit UI -- a face for the control mapper.

Run with:
    streamlit run app.py

Three tabs (phase 1): Map (one framework, quick look), Crosswalk (compare two or three
frameworks over the same statements, draft downloads) and Review (decide on every
mapping of the latest crosswalk, accepted downloads). The sidebar toggle runs offline
(stub client) for a zero-cost demo, or against the live model with ANTHROPIC_API_KEY.

This file is intentionally thin: all logic lives in the `control_mapper` package
and is unit-tested. The UI only orchestrates and displays. Results live in
st.session_state so they survive Streamlit's rerun-on-every-click. User and model
text (statements, rationale, reasons, policy name) is only ever shown through
st.dataframe or st.text, never through Markdown.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from control_mapper import (  # noqa: E402
    ControlMapper,
    CrosswalkInput,
    analyze,
    build_crosswalk,
    equivalences_for,
    load_framework,
    parse_policy,
)
from control_mapper import review as rv  # noqa: E402
from control_mapper.export import (  # noqa: E402
    crosswalk_csv_bytes,
    crosswalk_xlsx_bytes,
)
from control_mapper.models import CoverageLevel  # noqa: E402

# label -> (framework JSON, short name used in the UI)
FRAMEWORK_FILES = {
    "NIST SP 800-53 Rev. 5": (
        ROOT / "data" / "frameworks" / "nist_800-53_subset.json",
        "NIST 800-53",
    ),
    "ISO/IEC 27001:2022 Annex A": (
        ROOT / "data" / "frameworks" / "iso_27001_2022_annexa_subset.json",
        "ISO 27001",
    ),
    "SOC 2 Trust Services Criteria": (
        ROOT / "data" / "frameworks" / "soc2_tsc_subset.json",
        "SOC 2",
    ),
}
ALL_LABELS = list(FRAMEWORK_FILES)
SAMPLE_PATH = ROOT / "data" / "sample_policies" / "sample_policy.md"
POLICY_NAME_DEFAULT = "Acme Corp Information Security Policy (excerpt)"
# One constant for the tab bar; phase 2 tabs are placeholders only.
TABS = ["Map", "Crosswalk", "Review", "Controls", "Plan", "Verify", "Dashboard"]
PHASE2_TABS = TABS[3:]
LOW_CONFIDENCE = 0.75
DECISIONS = ["Accepted", "Rejected", "Pending"]
DECISION_FILTER_OPTIONS = ["Pending", "Accepted", "Rejected"]
STATUS_OF = {
    "Accepted": rv.ReviewStatus.ACCEPTED,
    "Rejected": rv.ReviewStatus.REJECTED,
    "Pending": rv.ReviewStatus.PENDING,
}
LABEL_OF_STATUS = {v: k for k, v in STATUS_OF.items()}
PANEL_KEYS = ("review_decision", "review_coverage", "review_reason")

REASON_ERROR = "Add a reason. A reason is required when you reject a mapping or change its coverage."
NO_KEY = (
    "Live mode needs an API key, and none is set. Turn on **Offline demo** in "
    "Settings, or set the key and restart the app."
)
NO_STATEMENTS = (
    "No numbered statements found. Number each requirement on its own line: 1., 2., 3."
)
XW_OUT_OF_DATE = (
    "Out of date. The policy name, policy text or frameworks changed after this run. "
    "Run the crosswalk again to update."
)
REVIEW_OUT_OF_DATE = (
    "Out of date. The policy name, policy text or frameworks changed after this run. "
    "Your decisions apply to the old run; export them now or run the crosswalk again."
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
NOT_LOOPBACK = (
    "This app only runs on a loopback address (127.0.0.1, ::1 or localhost). "
    "Set server.address in .streamlit/config.toml and restart."
)
MAP_OUT_OF_DATE = (
    "Out of date. The framework or policy text changed after this run. "
    "Select Map controls again to update."
)

FRAMEWORKS = {
    label: load_framework(path) for label, (path, _short) in FRAMEWORK_FILES.items()
}
SHORT_BY_ID = {
    FRAMEWORKS[label].framework_id: short
    for label, (_p, short) in FRAMEWORK_FILES.items()
}
LABEL_BY_ID = {fw.framework_id: label for label, fw in FRAMEWORKS.items()}

ss = st.session_state


# ---------------------------------------------------------------------------
# helpers (pure display/derivation; no user text through Markdown)
# ---------------------------------------------------------------------------


def get_client(offline: bool):
    if offline:
        from eval.stub_client import StubClient

        return StubClient()
    from control_mapper import AnthropicClient

    return AnthropicClient()


def _duplicate_labels(statements) -> list:
    """Statement numbers used more than once (labels only, never statement text)."""
    seen, dups = set(), []
    for s in statements:
        if s.id in seen and s.id not in dups:
            dups.append(s.id)
        seen.add(s.id)
    return dups


def _duplicate_error(dups) -> str:
    # Labels come from parse_policy ("P" + digits), so they are safe in st.error.
    return (
        f"Duplicate statement number: {', '.join(dups)} used more than once. "
        "Number each statement once (1., 2., 3.) and try again."
    )


def _input_hash(name: str, text: str, labels) -> str:
    blob = json.dumps([name, text, list(labels)], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _current_hash() -> str:
    return _input_hash(
        ss.get("policy_name", ""),
        ss.get("policy_text", ""),
        ss.get("xw_frameworks", []),
    )


def _source_url(fw, control_id=None) -> str:
    ctrl = fw.get(control_id) if control_id else None
    return (ctrl.source_url if ctrl else None) or fw.source_url or ""


def _sources_line(frameworks) -> str:
    # Framework names and URLs come from the framework JSON (URLs are allowlisted at load).
    links = [
        f"[{SHORT_BY_ID.get(fw.framework_id, fw.framework_id)} ↗]({fw.source_url})"
        for fw in frameworks
        if fw.source_url
    ]
    return "Framework sources: " + " · ".join(links) if links else ""


def _conf_text(conf: float) -> str:
    return f"{conf:.2f}" + (" ⚠ low" if conf < LOW_CONFIDENCE else "")


def _crosswalk_config(framework_ids) -> dict:
    """Fixed pixel widths that fit three frameworks in the ~1000 px content area.

    Cells list one control per line.
    """
    cfg = {
        "Statement": st.column_config.TextColumn(width=70),
        "Text": st.column_config.TextColumn(width=300),
    }
    for fid in framework_ids:
        cfg[SHORT_BY_ID.get(fid, fid)] = st.column_config.TextColumn(
            width=200,
            help="Controls for this framework, one per line.",
        )
    return cfg


def _order_key(label: str):
    digits = "".join(ch for ch in label if ch.isdigit())
    return (int(digits) if digits else 0, label)


def _decided(state) -> list:
    return [it for it in state.items.values() if it.status != rv.ReviewStatus.PENDING]


def _pick_value(item) -> str:
    return f"{item.mapping.statement_id}|{item.framework_id}|{item.mapping.control_id}"


def _key_of_pick(state, pick: str):
    label, fid, cid = pick.split("|")
    return rv.key_for(state, fid, label, cid)


def _sorted_items(state, run):
    fw_order = {inp.framework.framework_id: i for i, inp in enumerate(run["inputs"])}
    return sorted(
        state.items.values(),
        key=lambda it: (
            _order_key(it.mapping.statement_id),
            fw_order.get(it.framework_id, 99),
            it.mapping.control_id,
        ),
    )


def _view(state, run):
    """Items in table order after the Review filters."""
    fw_ids = {
        FRAMEWORKS[lbl].framework_id for lbl in ss.get("review_filter_framework", [])
    }
    statuses = {STATUS_OF[d] for d in ss.get("review_filter_decision", [])}
    low_only = ss.get("review_filter_low", False)
    return [
        it
        for it in _sorted_items(state, run)
        if it.framework_id in fw_ids
        and it.status in statuses
        and (not low_only or it.mapping.confidence < LOW_CONFIDENCE)
    ]


def _view_default(view) -> str | None:
    for it in view:
        if it.status == rv.ReviewStatus.PENDING:
            return _pick_value(it)
    return _pick_value(view[0]) if view else None


def _reset_panel(state, pick) -> None:
    """Panel widgets follow the picked item's stored values; the reason starts blank."""
    if pick is None:
        for k in PANEL_KEYS:
            ss.pop(k, None)
        return
    item = state.items[_key_of_pick(state, pick)]
    ss["review_decision"] = LABEL_OF_STATUS[item.status]
    ss["review_coverage"] = item.coverage.value
    ss["review_reason"] = ""


def _reset_review_widgets(run) -> None:
    ss["review_filter_framework"] = [
        LABEL_BY_ID[i.framework.framework_id] for i in run["inputs"]
    ]
    ss["review_filter_decision"] = list(DECISION_FILTER_OPTIONS)
    ss["review_filter_low"] = False
    for k in ("review_pick",) + PANEL_KEYS:
        ss.pop(k, None)


# ---------------------------------------------------------------------------
# callbacks (run before the script, so widget state can be set safely)
# ---------------------------------------------------------------------------


def _on_run_click() -> None:
    old = ss.get("review")
    duplicates = _duplicate_labels(parse_policy(ss.get("policy_text", "")))
    if old is not None and _decided(old) and not duplicates:
        ss["confirm_rerun"] = True
    else:
        ss["xw_do_run"] = True


def _on_confirm_rerun() -> None:
    ss["confirm_rerun"] = False
    ss["xw_do_run"] = True


def _on_cancel_rerun() -> None:
    ss["confirm_rerun"] = False


def _on_filter_change() -> None:
    state, run = ss.get("review"), ss.get("crosswalk_run")
    if state is None or run is None:
        return
    pick = _view_default(_view(state, run))
    if pick is None:
        ss.pop("review_pick", None)
    else:
        ss["review_pick"] = pick
    _reset_panel(state, pick)


def _on_pick_change() -> None:
    state = ss.get("review")
    if state is not None and ss.get("review_pick"):
        _reset_panel(state, ss["review_pick"])


def _on_save() -> None:
    state, run = ss.get("review"), ss.get("crosswalk_run")
    pick = ss.get("review_pick")
    if state is None or run is None or not pick:
        return
    key = _key_of_pick(state, pick)
    item = state.items[key]
    status = STATUS_OF[ss.get("review_decision", "Pending")]
    coverage = ss.get("review_coverage", item.coverage.value)
    reason = (ss.get("review_reason") or "").strip()

    changed_coverage = (
        status == rv.ReviewStatus.ACCEPTED and coverage != item.coverage.value
    )
    reason_required = status == rv.ReviewStatus.REJECTED or changed_coverage
    if reason_required and not reason:
        ss["review_msg"] = ("error", REASON_ERROR)
        return
    reason_value = reason or None
    unchanged = (
        status == item.status
        and not changed_coverage
        and (reason_value is None or reason_value == item.reason)
    )
    if unchanged:
        ss["review_msg"] = (
            "info",
            "Nothing to save. Change the decision, the coverage or the reason first.",
        )
        return
    now = datetime.now(UTC)
    try:
        if changed_coverage:
            rv.set_coverage(
                state, key, CoverageLevel(coverage), reason=reason_value, at=now
            )
        rv.set_status(state, key, status, reason=reason_value, at=now)
    except ValueError:
        ss["review_msg"] = ("error", REASON_ERROR)
        return

    # Move to the next pending mapping in the view (after this one, wrapping).
    view = _view(state, run)
    picks = [_pick_value(it) for it in view]
    pos = picks.index(pick) if pick in picks else -1
    ordered = view[pos + 1 :] + view[: pos + 1]
    nxt = next((it for it in ordered if it.status == rv.ReviewStatus.PENDING), None)
    done = (
        f"Saved. {item.mapping.statement_id} · {SHORT_BY_ID.get(item.framework_id, '')}"
        f" · {item.mapping.control_id} is now {LABEL_OF_STATUS[status]}."
    )
    if nxt is not None:
        new_pick = _pick_value(nxt)
        ss["review_msg"] = ("success", done + " Next pending mapping picked.")
    else:
        new_pick = pick if pick in picks else (picks[0] if picks else None)
        ss["review_msg"] = ("success", done + " No pending mappings left in this view.")
    if new_pick is None:
        ss.pop("review_pick", None)
    else:
        ss["review_pick"] = new_pick
    _reset_panel(state, new_pick)


# ---------------------------------------------------------------------------
# crosswalk run
# ---------------------------------------------------------------------------


def _execute_crosswalk(labels, policy_name: str, policy_text: str) -> None:
    statements = parse_policy(policy_text)
    if not statements:
        ss["xw_error"] = NO_STATEMENTS
        return
    duplicates = _duplicate_labels(statements)
    if duplicates:
        ss["xw_error"] = _duplicate_error(duplicates)
        return
    try:
        client = get_client(ss.get("offline", True))
    except RuntimeError:
        ss["xw_error"] = NO_KEY
        return

    inputs, errors = [], {}
    mapper = ControlMapper(client)
    for label in labels:
        fw = FRAMEWORKS[label]
        try:
            inputs.append(CrosswalkInput(fw, mapper.map(statements, fw)))
        except Exception as exc:  # one framework failing must not lose the rest
            errors[label] = type(exc).__name__
    if not inputs:
        ss["xw_error"] = (
            "No framework could be mapped. Run the crosswalk again to retry."
        )
        return

    old = ss.get("review")
    new = rv.start_review(policy_name, statements, inputs, previous=old)
    carry = None
    if old is not None and _decided(old):
        new_skeys = set(new.labels.values())
        estimate = sum(
            1
            for k, it in old.items.items()
            if it.status != rv.ReviewStatus.PENDING and k[:3] in new_skeys
        )
        carried = len(_decided(new))
        carry = {"kept": carried, "dropped": max(estimate - carried, 0)}
    run = {
        "statements": statements,
        "inputs": inputs,
        "frameworks": list(labels),
        "anchor": ss.get("xw_anchor"),
        "policy_name": policy_name,
        "input_hash": _input_hash(policy_name, policy_text, labels),
        "errors": errors,
        "carry": carry,
    }
    ss["crosswalk_run"] = run
    ss["review"] = new
    _reset_review_widgets(run)


def _confirm_text(state, policy_name: str, policy_text: str) -> str:
    decided = [
        (k, it) for k, it in state.items.items() if it.status != rv.ReviewStatus.PENDING
    ]
    if policy_name != state.policy_id:
        return (
            "Run the crosswalk again? The policy name changed, so decisions do not carry "
            f"over. 0 decisions kept, {len(decided)} reset to pending (policy name changed)."
        )
    new_skeys = set(rv.statement_keys(policy_name, parse_policy(policy_text)).values())
    kept = sum(1 for k, _ in decided if k[:3] in new_skeys)
    word = "decision" if kept == 1 else "decisions"
    return (
        "Run the crosswalk again? Decisions carry over where the statement text has not "
        f"changed. {kept} {word} kept, {len(decided) - kept} reset to pending "
        "(statement changed)."
    )


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Control Mapper", layout="wide")

# Startup guard: refuse to render anything unless bound to loopback (unset = refuse).
if (st.get_option("server.address") or "") not in LOOPBACK_HOSTS:
    st.error(NOT_LOOPBACK)
    st.stop()
st.title("Policy → Control Mapper")
st.caption(
    "Maps organizational policy statements to compliance framework controls, "
    "then flags coverage gaps and out-of-scope statements for human review."
)

with st.sidebar:
    st.header("Settings")
    offline = st.toggle("Offline demo (no API key)", value=True, key="offline")
    st.markdown(
        "Offline mode uses a deterministic stub, so you can demo without an API key "
        "or network. Turn it off to run against Claude (needs `ANTHROPIC_API_KEY`). "
        "Live mode sends your policy text to Anthropic."
    )
    st.divider()
    st.markdown(
        "**Reminder:** mappings are AI-assisted drafts. A qualified human should "
        "review every mapping before it is used as audit evidence."
    )

policy_name = st.text_input(
    "Policy name",
    value=POLICY_NAME_DEFAULT,
    key="policy_name",
    max_chars=120,
    help="Names this policy in exports. Keep the same name when you edit the text, "
    "so your decisions carry over.",
)
policy_text = st.text_area(
    "Policy text (numbered statements)",
    SAMPLE_PATH.read_text(encoding="utf-8"),
    height=280,
    key="policy_text",
)
st.caption(f"{len(parse_policy(policy_text))} numbered statements found.")

tabs = dict(zip(TABS, st.tabs(TABS, key="view", on_change="rerun")))

# --- Map tab ---------------------------------------------------------------
with tabs["Map"]:
    framework_label = st.selectbox("Framework", ALL_LABELS, key="map_framework")
    if st.button("Map controls", type="primary"):
        statements = parse_policy(policy_text)
        duplicates = _duplicate_labels(statements)
        if not statements:
            st.error(NO_STATEMENTS)
        elif duplicates:
            st.error(_duplicate_error(duplicates))
        else:
            try:
                client = get_client(offline)
            except RuntimeError:
                st.error(NO_KEY)
            else:
                fw = FRAMEWORKS[framework_label]
                with st.spinner("Mapping..."):
                    ss["map_result"] = ControlMapper(client).map(statements, fw)
                ss["map_run"] = {
                    "framework": framework_label,
                    "policy_text": policy_text,
                    "statements": statements,
                }

    result, meta = ss.get("map_result"), ss.get("map_run")
    if result is not None and meta is not None:
        if (meta["framework"], meta["policy_text"]) != (framework_label, policy_text):
            st.warning(MAP_OUT_OF_DATE)
        fw = FRAMEWORKS[meta["framework"]]
        statements = meta["statements"]
        analysis = analyze(result, fw, statements)
        covered = len(analysis.covered_controls) + len(analysis.partial_controls)
        c1, c2, c3 = st.columns(3)
        c1.metric("Framework coverage", f"{analysis.coverage_ratio:.0%}")
        c2.metric("Controls covered", f"{covered}/{len(fw.controls)}")
        c3.metric("Unmapped statements", len(analysis.unmapped_statements))

        st.subheader("Mappings")
        rows = []
        for m in result.mappings:
            ctrl = fw.get(m.control_id)
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
        st.dataframe(
            rows,
            width="stretch",
            hide_index=True,
            column_config={"Confidence": st.column_config.NumberColumn(format="%.2f")},
        )

        col_left, col_right = st.columns(2)
        with col_left:
            st.subheader("Uncovered controls (gaps)")
            if analysis.uncovered_controls:
                st.dataframe(
                    [
                        {
                            "Control": cid,
                            "Title": fw.get(cid).title if fw.get(cid) else "",
                            "Source": _source_url(fw, cid),
                        }
                        for cid in analysis.uncovered_controls
                    ],
                    hide_index=True,
                    column_config={
                        "Control": st.column_config.TextColumn(width="small"),
                        "Title": st.column_config.TextColumn(width="medium"),
                        "Source": st.column_config.LinkColumn(
                            display_text="Publisher page", width="small"
                        ),
                    },
                )
            else:
                st.text("None in scope.")
        with col_right:
            st.subheader("Unmapped statements")
            if analysis.unmapped_statements:
                text_by_id = {s.id: s.text for s in statements}
                st.dataframe(
                    [
                        {"Statement": sid, "Text": text_by_id.get(sid, "")}
                        for sid in analysis.unmapped_statements
                    ],
                    hide_index=True,
                )
            else:
                st.text("Every statement mapped to a control.")

# --- Crosswalk tab ---------------------------------------------------------
with tabs["Crosswalk"]:
    chosen = st.multiselect(
        "Frameworks to compare",
        ALL_LABELS,
        default=ALL_LABELS,
        max_selections=3,
        key="xw_frameworks",
        help="Pick two or three.",
    )
    if chosen and ss.get("xw_anchor") not in chosen:
        ss["xw_anchor"] = chosen[0]
    elif not chosen:
        ss.pop("xw_anchor", None)
    anchor_label = st.selectbox(
        "Anchor framework for equivalences",
        chosen,
        key="xw_anchor",
        help="Equivalences list each control in this framework and the matching "
        "controls in the others.",
    )
    name_ok = bool(policy_name.strip())
    if not name_ok:
        st.warning(
            "Enter a policy name. It labels your exports and keeps your decisions when "
            "you edit the text."
        )
    st.button(
        "Run crosswalk",
        type="primary",
        disabled=len(chosen) < 2 or not name_ok,
        on_click=_on_run_click,
    )
    if not chosen:
        st.info("Pick two or three frameworks, then select **Run crosswalk**.")
    elif len(chosen) < 2:
        st.info("Pick at least two frameworks to compare.")

    if ss.pop("xw_do_run", False) and len(chosen) >= 2 and name_ok:
        n_statements = len(parse_policy(policy_text))
        with st.spinner(
            f"Mapping {n_statements} statements against {len(chosen)} frameworks…"
        ):
            _execute_crosswalk(chosen, policy_name, policy_text)

    if ss.get("confirm_rerun") and ss.get("review") is not None:
        st.warning(_confirm_text(ss["review"], policy_name, policy_text))
        b1, b2 = st.columns(2)
        b1.button(
            "Run crosswalk again",
            key="rerun_confirm",
            type="primary",
            on_click=_on_confirm_rerun,
        )
        b2.button("Cancel", key="rerun_cancel", on_click=_on_cancel_rerun)

    xw_error = ss.pop("xw_error", None)
    if xw_error:
        st.error(xw_error)

    run = ss.get("crosswalk_run")
    state = ss.get("review")
    out_of_date = run is not None and run["input_hash"] != _current_hash()
    if run is not None and state is not None:
        inputs = run["inputs"]
        statements = run["statements"]
        if out_of_date:
            st.warning(XW_OUT_OF_DATE)
        elif run["errors"]:
            failed = ", ".join(
                f"{SHORT_BY_ID.get(FRAMEWORKS[lbl].framework_id, lbl)} failed: {why}"
                for lbl, why in run["errors"].items()
            )
            st.warning(
                f"Draft from {len(inputs)} of {len(run['frameworks'])} frameworks. "
                f"{failed}. Run the crosswalk again to retry."
            )
        else:
            n = len(state.items)
            line = (
                f"**Draft.** AI mappings, {len(_decided(state))} of {n} reviewed. "
                "Check them on the Review tab before using them as evidence."
            )
            carry = run.get("carry")
            if carry:
                k, d = carry["kept"], carry["dropped"]
                line += f" {k} {'decision' if k == 1 else 'decisions'} kept from the last run"
                if d:
                    line += (
                        f"; {d} dropped because the AI no longer proposes that control"
                    )
                line += "."
            st.info(line)

        fws = [i.framework for i in inputs]
        cols = st.columns(len(inputs))
        for col, inp in zip(cols, inputs):
            a = analyze(inp.result, inp.framework, statements)
            touched = len(a.covered_controls) + len(a.partial_controls)
            col.metric(
                SHORT_BY_ID.get(inp.framework.framework_id, ""),
                f"{a.coverage_ratio:.0%}",
            )
            col.caption(f"{touched} of {len(inp.framework.controls)} controls covered")

        cw = build_crosswalk(statements, inputs)
        st.subheader("Crosswalk")
        st.caption(
            "One row per policy statement. full = the statement meets the whole control; "
            "partial = it meets part of it."
        )
        st.dataframe(
            [
                {
                    "Statement": row.statement_id,
                    "Text": row.statement_text,
                    **{
                        SHORT_BY_ID.get(fid, fid): (
                            "\n".join(
                                f"{c} {cov}" for c, cov in row.by_framework.get(fid, [])
                            )
                            or "No control mapped"
                        )
                        for fid in cw.framework_ids
                    },
                }
                for row in cw.rows
            ],
            hide_index=True,
            column_config=_crosswalk_config(cw.framework_ids),
            row_height=70,  # room for one control per line
        )
        sources = _sources_line(fws)
        if sources:
            st.markdown(sources)

        st.subheader("Uncovered controls (gaps)")
        st.caption("Controls in each framework that no statement covers.")
        for inp in inputs:  # stacked, one full-width table per framework
            fw = inp.framework
            a = analyze(inp.result, fw, statements)
            st.text(SHORT_BY_ID.get(fw.framework_id, ""))
            st.dataframe(
                [
                    {
                        "Control": cid,
                        "Title": fw.get(cid).title if fw.get(cid) else "",
                        "Source": _source_url(fw, cid),
                    }
                    for cid in a.uncovered_controls
                ],
                hide_index=True,
                column_config={
                    "Control": st.column_config.TextColumn(width="small"),
                    "Title": st.column_config.TextColumn(width="medium"),
                    "Source": st.column_config.LinkColumn(
                        display_text="Publisher page", width="small"
                    ),
                },
            )

        st.subheader("Unmapped statements")
        st.caption(
            "Statements with no control in a framework. Check whether they are out of "
            "scope or the framework needs more controls."
        )
        unmapped = []
        for row in cw.rows:
            missing = [
                SHORT_BY_ID.get(f, f)
                for f in cw.framework_ids
                if not row.by_framework.get(f)
            ]
            if missing:
                unmapped.append(
                    {
                        "Statement": row.statement_id,
                        "Text": row.statement_text,
                        "No control in": ", ".join(missing),
                    }
                )
        st.dataframe(unmapped, hide_index=True)

        anchor_fw = FRAMEWORKS.get(anchor_label) if anchor_label else None
        if anchor_fw is not None and anchor_fw.framework_id in cw.framework_ids:
            anchor_id = anchor_fw.framework_id
            with st.expander(
                f"Control equivalences (anchor: {SHORT_BY_ID.get(anchor_id, '')})",
                expanded=False,
            ):
                st.caption(
                    "Source links point to the anchor framework. Two controls are "
                    'equivalent when the same policy statement meets both. "via" names '
                    "that statement."
                )
                equivs = equivalences_for(cw, anchor_id)
                eq_rows = []
                for sc in sorted(equivs):
                    for tid, controls in equivs[sc].items():
                        eq_rows.append(
                            {
                                "Anchor control": sc,
                                "Source": _source_url(anchor_fw, sc),
                                "Framework": SHORT_BY_ID.get(tid, tid),
                                "Equivalent controls": ", ".join(
                                    f"{tc} (via {','.join(stmts)})"
                                    for tc, stmts in sorted(controls.items())
                                )
                                or "No equivalent found",
                            }
                        )
                st.dataframe(
                    eq_rows,
                    hide_index=True,
                    column_config={
                        "Source": st.column_config.LinkColumn(
                            display_text="Publisher page"
                        )
                    },
                )

        st.subheader("Download")
        d1, d2, _ = st.columns([1, 1, 2])
        d1.download_button(
            "Download draft CSV",
            data=crosswalk_csv_bytes(cw, statements, inputs),
            file_name="crosswalk-draft.csv",
            mime="text/csv",
            on_click="ignore",
        )
        d2.download_button(
            "Download draft Excel",
            data=crosswalk_xlsx_bytes(cw, statements, inputs),
            file_name="crosswalk-draft.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            on_click="ignore",
        )
        st.caption(
            "Draft files contain every AI mapping. To export reviewed mappings only, "
            "use the Review tab."
        )

# --- Review tab ------------------------------------------------------------
with tabs["Review"]:
    run = ss.get("crosswalk_run")
    state = ss.get("review")
    if run is None or state is None:
        st.info("Nothing to review yet. Run a crosswalk on the Crosswalk tab first.")
    else:
        inputs = run["inputs"]
        statements = run["statements"]
        text_by_id = {s.id: s.text for s in statements}
        fw_by_id = {i.framework.framework_id: i.framework for i in inputs}
        items = list(state.items.values())
        n = len(items)
        n_acc = sum(1 for it in items if it.status == rv.ReviewStatus.ACCEPTED)
        n_rej = sum(1 for it in items if it.status == rv.ReviewStatus.REJECTED)
        n_pen = n - n_acc - n_rej
        n_low = sum(
            1
            for it in items
            if it.status == rv.ReviewStatus.PENDING
            and it.mapping.confidence < LOW_CONFIDENCE
        )

        if run["input_hash"] != _current_hash():
            st.warning(REVIEW_OUT_OF_DATE)
        elif n_pen:
            st.warning(
                f"**AI drafts until you review them.** {n - n_pen} of {n} mappings "
                f"reviewed; {n_pen} pending. Only accepted mappings are exported."
            )
        else:
            st.success(
                f"All {n} mappings reviewed: {n_acc} accepted, {n_rej} rejected. "
                "The accepted export is ready."
            )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Pending", n_pen)
        m2.metric("Accepted", n_acc)
        m3.metric("Rejected", n_rej)
        m4.metric("Low confidence, pending", n_low)
        st.progress((n - n_pen) / n if n else 0.0)
        st.caption(f"{n - n_pen} of {n} reviewed")

        run_labels = [LABEL_BY_ID[i.framework.framework_id] for i in inputs]
        if "review_filter_framework" not in ss:
            ss["review_filter_framework"] = run_labels
        if "review_filter_decision" not in ss:
            ss["review_filter_decision"] = list(DECISION_FILTER_OPTIONS)
        st.subheader("Mappings")
        st.multiselect(
            "Framework",
            run_labels,
            key="review_filter_framework",
            on_change=_on_filter_change,
        )
        st.multiselect(
            "Decision",
            DECISION_FILTER_OPTIONS,
            key="review_filter_decision",
            on_change=_on_filter_change,
        )
        st.toggle(
            "Low confidence only (below 0.75)",
            key="review_filter_low",
            on_change=_on_filter_change,
        )

        view = _view(state, run)
        st.caption(
            f"Showing {len(view)} of {n} mappings. Pick a mapping below to decide on it."
        )
        if not view:
            st.info("No mappings match these filters. Clear a filter to see more.")
        else:
            options = [_pick_value(it) for it in view]
            if ss.get("review_pick") not in options:
                ss["review_pick"] = _view_default(view)
                _reset_panel(state, ss["review_pick"])
            elif any(k not in ss for k in PANEL_KEYS):
                _reset_panel(state, ss["review_pick"])

            with st.container(border=True):
                st.subheader("Decide on a mapping")
                pick = st.selectbox(
                    "Mapping",
                    options,
                    key="review_pick",
                    on_change=_on_pick_change,
                    help="Lists the mappings in this view, in table order. The first "
                    "pending one is picked for you.",
                )
                item = state.items[_key_of_pick(state, pick)]
                fw = fw_by_id.get(item.framework_id)
                ctrl = fw.get(item.mapping.control_id) if fw else None
                label = item.mapping.statement_id

                # Compact grid: labels are static literals (st.markdown); every value,
                # including user and model text, goes through st.text only.
                def _detail(static_label: str, value: str) -> None:
                    label_col, value_col = st.columns([1, 4])
                    label_col.markdown(static_label)
                    value_col.text(value)

                _detail("**Statement**", f"{label} · {text_by_id.get(label, '')}")
                _detail(
                    "**Control**",
                    f"{SHORT_BY_ID.get(item.framework_id, '')} · "
                    f"{item.mapping.control_id} {ctrl.title if ctrl else ''}",
                )
                url = _source_url(fw, item.mapping.control_id) if fw else ""
                if url:
                    # URL comes from the allowlisted framework JSON, never from user/model.
                    src_label, src_value = st.columns([1, 4])
                    src_label.markdown("**Source**")
                    src_value.link_button("Publisher page ↗", url)
                _detail(
                    "**AI coverage**",
                    f"{item.mapping.coverage.value} · confidence "
                    f"{_conf_text(item.mapping.confidence)}",
                )
                _detail("**Rationale**", item.mapping.rationale)
                _detail(
                    "**Decision now**",
                    f"{LABEL_OF_STATUS[item.status]} · coverage {item.coverage.value}",
                )
                _detail("**Reason now**", item.reason or "None given")
                if item.coverage_reason:
                    _detail("**Coverage reason**", item.coverage_reason)

                decision = st.radio(
                    "Your decision", DECISIONS, key="review_decision", horizontal=True
                )
                coverage = st.selectbox(
                    "Coverage",
                    ["full", "partial"],
                    key="review_coverage",
                    disabled=decision != "Accepted",
                    help="Set coverage when you accept. Change it only if the AI got it "
                    "wrong; a change needs a reason.",
                )
                if decision == "Rejected":
                    reason_help = "Required: you are rejecting this mapping."
                elif decision == "Accepted" and coverage != item.coverage.value:
                    reason_help = (
                        f"Required: you are changing coverage from {item.coverage.value} "
                        f"to {coverage}."
                    )
                else:
                    reason_help = (
                        "Required when you reject or change coverage; optional otherwise. "
                        "Saved with the decision and exported."
                    )
                st.text_input(
                    "Reason",
                    key="review_reason",
                    max_chars=500,
                    placeholder="Why you decided this, in one sentence",
                    help=reason_help,
                )
                st.button(
                    "Save decision",
                    key="review_save",
                    type="primary",
                    on_click=_on_save,
                )
                msg = ss.pop("review_msg", None)
                if msg:
                    kind, text = msg
                    {"error": st.error, "info": st.info, "success": st.success}[kind](
                        text
                    )

        st.subheader("Mappings in this view")
        st.caption("Read only. Decide with the panel above.")
        st.dataframe(
            [
                {
                    "Decision": LABEL_OF_STATUS[it.status],
                    "Statement": it.mapping.statement_id,
                    "Framework": SHORT_BY_ID.get(it.framework_id, it.framework_id),
                    "Control": it.mapping.control_id,
                    "Control title": (
                        fw_by_id[it.framework_id].get(it.mapping.control_id).title
                        if it.framework_id in fw_by_id
                        and fw_by_id[it.framework_id].get(it.mapping.control_id)
                        else ""
                    ),
                    "Source": (
                        _source_url(fw_by_id[it.framework_id], it.mapping.control_id)
                        if it.framework_id in fw_by_id
                        else ""
                    ),
                    "Confidence": _conf_text(it.mapping.confidence),
                    "Coverage": it.coverage.value,
                    "AI coverage": it.mapping.coverage.value,
                    "Reason": it.reason or "",
                    "Coverage reason": it.coverage_reason or "",
                    "Rationale": it.mapping.rationale,
                    "Statement text": text_by_id.get(it.mapping.statement_id, ""),
                }
                for it in view
            ],
            key="review_table",
            hide_index=True,
            column_config={
                "Source": st.column_config.LinkColumn(display_text="Publisher page")
            },
        )

        # Accepted crosswalk: rebuilt from accepted mappings only, every rerun.
        acc = rv.accepted_inputs(state, inputs)
        acc_cw = build_crosswalk(statements, acc)
        pending = {}
        for it in items:
            if it.status == rv.ReviewStatus.PENDING:
                cell = (it.mapping.statement_id, it.framework_id)
                pending.setdefault(cell, []).append(it.mapping.control_id)

        st.subheader("Accepted crosswalk")
        st.caption(
            "Built from accepted mappings only, using the edited coverage. “Pending "
            "review” means the AI proposed a control that nobody has decided on yet; “No "
            "control mapped” means there is none. Rejected mappings are left out."
        )
        if not n_acc:
            st.text("No accepted mappings yet.")
        cols = st.columns(len(acc))
        for col, inp in zip(cols, acc):
            fw = inp.framework
            a = analyze(inp.result, fw, statements)
            touched = len(a.covered_controls) + len(a.partial_controls)
            p_stmts = {lbl for (lbl, fid) in pending if fid == fw.framework_id}
            u_stmts = [s for s in a.unmapped_statements if s not in p_stmts]
            col.metric(SHORT_BY_ID.get(fw.framework_id, ""), f"{a.coverage_ratio:.0%}")
            col.caption(
                f"{touched} of {len(fw.controls)} controls covered · {len(p_stmts)} "
                f"statements pending review · {len(u_stmts)} unmapped statement(s)"
            )

        def _acc_cell(row, fid):
            pairs = row.by_framework.get(fid, [])
            waiting = sorted(pending.get((row.statement_id, fid), []))
            text = "\n".join(f"{c} {cov}" for c, cov in pairs)
            if pairs and waiting:
                return f"{text}\n+ pending: {', '.join(waiting)}"
            if pairs:
                return text
            if waiting:
                return f"Pending review: {', '.join(waiting)}"
            return "No control mapped"

        st.dataframe(
            [
                {
                    "Statement": row.statement_id,
                    "Text": row.statement_text,
                    **{
                        SHORT_BY_ID.get(f, f): _acc_cell(row, f)
                        for f in acc_cw.framework_ids
                    },
                }
                for row in acc_cw.rows
            ],
            hide_index=True,
            column_config=_crosswalk_config(acc_cw.framework_ids),
            row_height=70,  # room for one control per line
        )
        sources = _sources_line([i.framework for i in acc])
        if sources:
            st.markdown(sources)
        with st.expander("Controls not covered by accepted mappings"):
            pending_ctrls = {
                (it.framework_id, it.mapping.control_id)
                for it in items
                if it.status == rv.ReviewStatus.PENDING
            }
            gap_rows = []
            for inp in acc:
                fw = inp.framework
                touched = {m.control_id for m in inp.result.mappings}
                for ctrl in fw.controls:
                    if ctrl.id not in touched:
                        gap_rows.append(
                            {
                                "Framework": SHORT_BY_ID.get(fw.framework_id, ""),
                                "Control": ctrl.id,
                                "Title": ctrl.title,
                                "Status": (
                                    "Pending review"
                                    if (fw.framework_id, ctrl.id) in pending_ctrls
                                    else "Uncovered"
                                ),
                                "Source": _source_url(fw, ctrl.id),
                            }
                        )
            st.dataframe(
                gap_rows,
                hide_index=True,
                column_config={
                    "Title": st.column_config.TextColumn(width="medium"),
                    "Source": st.column_config.LinkColumn(
                        display_text="Publisher page", width="small"
                    ),
                },
            )

        has_acc = n_acc > 0
        st.subheader("Download")
        a1, a2, _ = st.columns([1, 1, 2])
        disabled_help = None if has_acc else "Accept at least one mapping to export."
        a1.download_button(
            "Download accepted CSV",
            data=(
                crosswalk_csv_bytes(acc_cw, statements, acc, review=state)
                if has_acc
                else b""
            ),
            file_name="crosswalk-accepted.csv",
            mime="text/csv",
            disabled=not has_acc,
            help=disabled_help,
            on_click="ignore",
        )
        a2.download_button(
            "Download accepted Excel",
            data=(
                crosswalk_xlsx_bytes(acc_cw, statements, acc, review=state)
                if has_acc
                else b""
            ),
            file_name="crosswalk-accepted.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            disabled=not has_acc,
            help=disabled_help,
            on_click="ignore",
        )
        st.caption(
            f"Files: crosswalk-accepted.csv, crosswalk-accepted.xlsx ({n_acc} mappings, "
            "with decision, coverage, AI coverage and reason). "
            f"{n_pen} pending and {n_rej} rejected mappings are not included."
        )
        st.button(
            "Add to remediation plan",
            disabled=True,
            help="Coming with the Plan tab: send accepted partial mappings and uncovered "
            "controls to a remediation plan.",
        )

# --- Phase 2 placeholders --------------------------------------------------
for name in PHASE2_TABS:
    with tabs[name]:
        st.caption("Coming in phase 2.")
