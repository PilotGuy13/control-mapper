"""Red UI tests: app.py driven headless with streamlit.testing.v1.AppTest, offline only.

Source of truth for the UI: review-ui-mockups/notes.md (designer, DRAFT awaiting John).
Tests assert behaviour, not layout. Every label / key / session key the tests rely on
is in the CONTRACT CONSTANTS block directly below -- rename there, nowhere else.

UI CONTRACT
===========
Tabs                 "Map", "Crosswalk", "Review" must be PRESENT (phase 2 appends more).
Shared               st.text_area(key="policy_text") above the tabs; sidebar keeps the
                     offline toggle ("Offline demo (no API key)", default ON).
Map tab              framework st.selectbox labelled "Framework" lives IN the Map tab
                     (not the sidebar); button "Map controls"; metric "Framework coverage".
                     Result in session_state["map_result"] (MappingResult). Changing the
                     framework or policy text keeps the result and shows an "out of date"
                     st.warning (ASSUMPTION: notes.md gives no Map copy; Crosswalk copy reused).
Crosswalk tab        multiselect "Frameworks to compare" (default all three, max 3);
                     selectbox "Anchor framework for equivalences" (options = chosen; reset
                     to the first chosen if the anchor is deselected); button "Run crosswalk"
                     disabled when fewer than 2 chosen, with st.info "Pick at least two
                     frameworks to compare.". Draft downloads "Download draft CSV" /
                     "Download draft Excel" fed by export.crosswalk_*_bytes(cw, statements,
                     raw_inputs) (review=None).
                     Each framework is mapped in its own try/except: one failure -> partial
                     run, crosswalk_run["errors"][<label or framework_id>] set, st.warning
                     starting "Draft from {ok} of {k} frameworks.".
Review tab           filter multiselect "Decision"; downloads "Download accepted CSV" /
                     "Download accepted Excel", disabled until >= 1 accepted, fed by
                     export.crosswalk_*_bytes(build_crosswalk(statements, acc), statements,
                     acc, review=state) where acc == review.accepted_inputs(state, raw).
session_state        "crosswalk_run": dict with "inputs" (List[CrosswalkInput], raw AI
                     proposals), "errors" (dict), "anchor"; "review": review.ReviewState.
Out of date          policy text or chosen frameworks change after a run -> results and
                     decisions KEPT, st.warning starting "Out of date." Anchor change is not
                     out of date.
Policy name          st.text_input key "policy_name", default "Acme Corp Information
                     Security Policy (excerpt)", is the review policy_id -- NOT a hash of
                     the policy text. Blank (or whitespace) -> "Run crosswalk" disabled.
                     A changed name is also "Out of date".
Re-run guard         "Run crosswalk" with >= 1 Accepted/Rejected decision shows a confirm
                     (buttons "Run crosswalk again" key "rerun_confirm", "Cancel" key
                     "rerun_cancel") whose text includes "N decisions kept, M reset to
                     pending (statement changed)" -- or, if the policy name changed,
                     "0 decisions kept, M reset to pending (policy name changed)" and
                     nothing carries. Confirm = re-run + review.start_review(...,
                     previous=old) by stable key; Cancel = no re-run, nothing touched.
Decide panel         ONLY write path (notes.md v2 §4). Keys: review_pick (selectbox,
                     options "P5|<framework_id>|SC-7" -> review.key_for), review_decision
                     (radio), review_coverage (selectbox, disabled unless Accepted),
                     review_reason (text_input), review_save (button). Reason required
                     when Rejected or coverage differs from the CURRENT stored coverage ->
                     st.error "Add a reason. ..." and nothing saved; unchanged ->
                     st.info "Nothing to save. ..."; saved -> st.success "Saved. ...".
                     Changing the picker or any Review filter resets decision/coverage/
                     reason to the picked item's stored values; filters reset the picker
                     to the view's first Pending (else first row). Table key
                     "review_table" is a read-only st.dataframe; no st.data_editor.
Decisions            keyed by the stable ADR-0001 D2 key (policy_id, statement_hash,
                     occurrence, framework_id, control_id) via review.py; survive filter
                     changes and reruns.

Not testable in AppTest 1.67 (stated gaps): download payload bytes (only label/disabled
exposed), tab-jump behaviour, row highlight.

Offline fence: ANTHROPIC_API_KEY removed and every live client constructor patched to
raise, so a test can never reach the live model or spend money.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# ---- CONTRACT CONSTANTS (keep in sync with the docstring / notes.md) -----------------
TABS = ("Map", "Crosswalk", "Review")
OFFLINE_TOGGLE_PREFIX = "Offline"
POLICY_KEY = "policy_text"
MAP_FRAMEWORK_LABEL = "Framework"
MAP_BUTTON = "Map controls"
MAP_METRIC = "Framework coverage"
XW_FRAMEWORKS_LABEL = "Frameworks to compare"
XW_ANCHOR_LABEL = "Anchor framework for equivalences"
XW_RUN_LABEL = "Run crosswalk"
XW_FEWER_THAN_TWO = "Pick at least two frameworks to compare."
XW_EMPTY = "Pick two or three frameworks"  # notes.md "Empty" line; acceptable for zero
OUT_OF_DATE_PREFIX = "Out of date."
PARTIAL_PREFIX = "Draft from"
CONFIRM_RUN_LABEL = "Run crosswalk again"   # notes.md v2 §7 -- change HERE only
CANCEL_LABEL = "Cancel"
RERUN_CONFIRM_KEY = "rerun_confirm"
RERUN_CANCEL_KEY = "rerun_cancel"
CONFIRM_COUNTS_RE = __import__("re").compile(
    r"(\d+) decisions? kept, (\d+) reset to pending \(statement changed\)")
CONFIRM_RENAMED_RE = __import__("re").compile(
    r"(\d+) decisions? kept, (\d+) reset to pending \(policy name changed\)")
POLICY_NAME_KEY = "policy_name"
POLICY_NAME_DEFAULT = "Acme Corp Information Security Policy (excerpt)"
PICK_KEY = "review_pick"                 # selectbox; option "P5|<framework_id>|SC-7"
DECISION_KEY = "review_decision"         # radio: Accepted / Rejected / Pending
COVERAGE_KEY = "review_coverage"         # selectbox full/partial; disabled unless Accepted
REASON_KEY = "review_reason"             # text_input
SAVE_KEY = "review_save"                 # button "Save decision"
REASON_ERROR = "Add a reason. A reason is required when you reject a mapping or change its coverage."
NOTHING_TO_SAVE = "Nothing to save."
SAVED_PREFIX = "Saved."
DRAFT_CSV_LABEL = "Download draft CSV"
DRAFT_XLSX_LABEL = "Download draft Excel"
ACCEPTED_CSV_LABEL = "Download accepted CSV"
ACCEPTED_XLSX_LABEL = "Download accepted Excel"
REVIEW_DECISION_FILTER_LABEL = "Decision"
SS_MAP = "map_result"
SS_RUN = "crosswalk_run"
SS_REVIEW = "review"
DUP_LABEL_ERROR = "duplicate statement number"  # st.error text contains this (any case)
NON_LOOPBACK_ERROR = "loopback"                 # st.error text contains this (any case)
# ---------------------------------------------------------------------------------------

APP = str(ROOT / "app.py")
NIST_L = "NIST SP 800-53 Rev. 5"
ISO_L = "ISO/IEC 27001:2022 Annex A"
SOC2_L = "SOC 2 Trust Services Criteria"
ALL_L = [NIST_L, ISO_L, SOC2_L]
LABEL_TO_ID = {
    NIST_L: "NIST_SP_800-53_R5",
    ISO_L: "ISO_IEC_27001_2022",
    SOC2_L: "SOC2_TSC_2017",
}
NIST_ID, ISO_ID = LABEL_TO_ID[NIST_L], LABEL_TO_ID[ISO_L]
# (framework_id, display label, control_id); translated to the stable ADR-0001 D2 key
# through review.key_for(state, ...) -- tests never assume labels are identity.
FP_KEY = (NIST_ID, "P5", "SC-7")
DOWNGRADE_KEY = (NIST_ID, "P2", "AC-2")
NEW_POLICY = "1. All staff complete security awareness training annually."
REASON = "test: reviewer reason"  # REJECTED and coverage edits require one


@pytest.fixture(autouse=True)
def never_live(monkeypatch):
    import anthropic

    import control_mapper
    import control_mapper.mapper

    def _boom(*a, **k):
        raise AssertionError("live Anthropic client constructed in an offline test")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", _boom)
    monkeypatch.setattr(control_mapper, "AnthropicClient", _boom)
    monkeypatch.setattr(control_mapper.mapper, "AnthropicClient", _boom)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(app: AppTest) -> AppTest:
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _new_app() -> AppTest:
    app = _run(AppTest.from_file(APP, default_timeout=30))
    assert _offline_toggle(app).value is True
    return app


@pytest.fixture
def at():
    return _new_app()


def _offline_toggle(app):
    return next(t for t in app.toggle if t.label.startswith(OFFLINE_TOGGLE_PREFIX))


def _by_label(widgets, label, what):
    found = [w for w in widgets if w.label == label]
    if not found:
        pytest.fail(f"{what} labelled {label!r} not rendered")
    return found[0]


def _button(app, label):
    return _by_label(app.button, label, "button")


def _download(app, label):
    return _by_label(app.download_button, label, "download_button")


def _has_label(widgets, label):
    return any(w.label == label for w in widgets)


def _metric_labels(app):
    return [m.label for m in app.metric]


def _warnings(app):
    return [str(w.value) for w in app.warning]


def _has_out_of_date(app):
    return any(w.startswith(OUT_OF_DATE_PREFIX) for w in _warnings(app))


def _policy(app):
    try:
        return app.text_area(key=POLICY_KEY)
    except KeyError:
        pytest.fail(f"text_area key {POLICY_KEY!r} not rendered")


def _map_framework(app):
    return _by_label(app.selectbox, MAP_FRAMEWORK_LABEL, "selectbox")


def _xw_frameworks(app):
    return _by_label(app.multiselect, XW_FRAMEWORKS_LABEL, "multiselect")


def _xw_anchor(app):
    return _by_label(app.selectbox, XW_ANCHOR_LABEL, "selectbox")


def _select_frameworks(app, labels):
    _xw_frameworks(app).set_value(labels)
    return _run(app)


def _run_crosswalk(app, labels=ALL_L, anchor=None):
    _select_frameworks(app, labels)
    if anchor is not None:
        _xw_anchor(app).set_value(anchor)
        _run(app)
    _button(app, XW_RUN_LABEL).click()
    return _run(app)


def _run_state(app):
    assert SS_RUN in app.session_state, f"session_state[{SS_RUN!r}] missing"
    return app.session_state[SS_RUN]


def _raw_inputs(app):
    return _run_state(app)["inputs"]


def _snap(inputs):
    return [(i.framework.framework_id, i.result.model_dump()) for i in inputs]


def _keyed(widgets, key, what):
    try:
        return widgets(key=key)
    except KeyError:
        pytest.fail(f"{what} key {key!r} not rendered")


def _has_key(widgets, key):
    try:
        widgets(key=key)
        return True
    except KeyError:
        return False


def _pick_value(triple):
    fid, label, cid = triple
    return f"{label}|{fid}|{cid}"


def _pick(app, triple):
    pick = _keyed(app.selectbox, PICK_KEY, "selectbox")
    want = _pick_value(triple)
    assert want in pick.options, f"{want!r} not in picker options"
    pick.set_value(want)
    return _run(app)


def _decide(app, triple, decision, coverage=None, reason="", expect="saved"):
    """Drive the Decide panel: pick, choose decision, optional coverage, reason, Save."""
    _pick(app, triple)
    _keyed(app.radio, DECISION_KEY, "radio").set_value(decision)
    _run(app)
    if coverage is not None:
        _keyed(app.selectbox, COVERAGE_KEY, "selectbox").set_value(coverage)
    _keyed(app.text_input, REASON_KEY, "text_input").input(reason)
    _keyed(app.button, SAVE_KEY, "button").click()
    _run(app)
    if expect == "saved":
        assert any(str(s.value).startswith(SAVED_PREFIX) for s in app.success), (
            f"save failed: errors={[e.value for e in app.error]}")
    return app


def _sk(app, triple):
    """(framework_id, display label, control_id) of the current run -> stable key."""
    from control_mapper import review

    return review.key_for(app.session_state[SS_REVIEW], *triple)


def _mapped(app):
    _button(app, MAP_BUTTON).click()
    _run(app)
    assert MAP_METRIC in _metric_labels(app)
    assert SS_MAP in app.session_state, "precondition: map_result stored after mapping"
    return app


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_tabs_map_crosswalk_review_present(at):
    labels = [t.label for t in at.tabs]
    assert set(TABS) <= set(labels), labels


def test_map_framework_picker_lives_in_map_tab_not_sidebar(at):
    assert not _has_label(at.sidebar.selectbox, MAP_FRAMEWORK_LABEL)
    map_tab = next((t for t in at.tabs if t.label == "Map"), None)
    assert map_tab is not None, "Map tab missing"
    assert _has_label(map_tab.selectbox, MAP_FRAMEWORK_LABEL)


def test_crosswalk_defaults_to_all_three_frameworks(at):
    assert list(_xw_frameworks(at).value) == ALL_L


# ---------------------------------------------------------------------------
# Map: persistence and out-of-date
# ---------------------------------------------------------------------------


def test_map_results_persist_across_rerun(at):
    _button(at, MAP_BUTTON).click()
    _run(at)
    assert MAP_METRIC in _metric_labels(at)
    _run(at)  # to Streamlit, any other widget interaction is a rerun without the click
    assert MAP_METRIC in _metric_labels(at), "results wiped by a rerun"
    assert SS_MAP in at.session_state
    result = at.session_state[SS_MAP]
    assert result.framework_id == NIST_ID
    assert len(result.mappings) > 0


def test_map_results_persist_when_crosswalk_selection_changes(at):
    _mapped(at)
    _select_frameworks(at, [ISO_L, SOC2_L])
    assert MAP_METRIC in _metric_labels(at)
    assert SS_MAP in at.session_state


def test_map_result_kept_and_flagged_when_framework_changes(at):
    _mapped(at)
    before = at.session_state[SS_MAP].model_dump()
    _map_framework(at).set_value(ISO_L)
    _run(at)
    assert SS_MAP in at.session_state and at.session_state[SS_MAP] is not None
    assert at.session_state[SS_MAP].model_dump() == before, (
        "stale map result must be kept, not recomputed or cleared")
    assert MAP_METRIC in _metric_labels(at)
    assert any("out of date" in w.lower() for w in _warnings(at))


def test_map_result_kept_and_flagged_when_policy_changes(at):
    _mapped(at)
    _policy(at).set_value(NEW_POLICY)
    _run(at)
    assert SS_MAP in at.session_state and at.session_state[SS_MAP] is not None
    assert MAP_METRIC in _metric_labels(at)
    assert any("out of date" in w.lower() for w in _warnings(at))


# ---------------------------------------------------------------------------
# Crosswalk: framework count {0, 1, 2, 3}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("labels", [[], [NIST_L]], ids=["zero", "one"])
def test_crosswalk_run_disabled_below_two_frameworks(at, labels):
    _select_frameworks(at, labels)
    assert _button(at, XW_RUN_LABEL).proto.disabled is True
    allowed = (XW_FEWER_THAN_TWO, XW_EMPTY) if not labels else (XW_FEWER_THAN_TWO,)
    assert any(a in str(i.value) for i in at.info for a in allowed)
    assert not at.error
    assert SS_RUN not in at.session_state


@pytest.mark.parametrize(
    "labels",
    [[NIST_L, ISO_L], [ISO_L, SOC2_L], ALL_L],
    ids=["nist+iso", "iso+soc2", "all-three"],
)
def test_crosswalk_runs_offline_for_two_or_three_frameworks(at, labels):
    _select_frameworks(at, labels)
    assert _button(at, XW_RUN_LABEL).proto.disabled is False
    _run_crosswalk(at, labels, anchor=labels[-1])
    inputs = _raw_inputs(at)
    assert {i.framework.framework_id for i in inputs} == {LABEL_TO_ID[x] for x in labels}
    assert all(len(i.result.mappings) > 0 for i in inputs)
    assert not _run_state(at)["errors"]


def test_anchor_options_follow_selection_and_reset_when_deselected(at):
    _select_frameworks(at, ALL_L)
    _xw_anchor(at).set_value(ISO_L)
    _run(at)
    _select_frameworks(at, [NIST_L, SOC2_L])
    anchor = _xw_anchor(at)
    assert set(anchor.options) == {NIST_L, SOC2_L}
    assert anchor.value == NIST_L  # reset to the first chosen framework


def _sweep(monkeypatch, name, real, spy, modules_by_name):
    """Replace `real` with `spy` wherever it is bound (identity sweep over sys.modules),
    and by name on the given modules (covers names that do not exist yet)."""
    if real is not None:
        for mod in list(sys.modules.values()):
            d = getattr(mod, "__dict__", None)
            if d is not None and d.get(name) is real:
                monkeypatch.setattr(mod, name, spy)
    for mod in modules_by_name:
        monkeypatch.setattr(mod, name, spy, raising=False)


def test_anchor_drives_equivalences(monkeypatch):
    import control_mapper
    import control_mapper.crosswalk as cwmod

    real = cwmod.equivalences_for
    seen = []

    def spy(crosswalk, source_framework_id):
        seen.append(source_framework_id)
        return real(crosswalk, source_framework_id)

    _sweep(monkeypatch, "equivalences_for", real, spy, (control_mapper, cwmod))
    app = _new_app()
    _run_crosswalk(app, ALL_L, anchor=ISO_L)
    assert seen, "equivalences_for never called for the crosswalk"
    assert seen[-1] == ISO_ID


def test_one_framework_failing_gives_partial_result_and_warning(monkeypatch):
    from eval import stub_client

    real = stub_client.StubClient.get_mappings

    def flaky(self, system, tool, user_content):
        if ISO_ID in user_content:
            raise RuntimeError("simulated upstream failure")
        return real(self, system, tool, user_content)

    monkeypatch.setattr(stub_client.StubClient, "get_mappings", flaky)
    app = _new_app()
    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    run = _run_state(app)
    assert {i.framework.framework_id for i in run["inputs"]} == {
        NIST_ID, LABEL_TO_ID[SOC2_L]}
    assert any(k in (ISO_L, ISO_ID) for k in run["errors"]), run["errors"]
    assert any(w.startswith(f"{PARTIAL_PREFIX} 2 of 3 frameworks") for w in _warnings(app))
    state = app.session_state[SS_REVIEW]
    assert len(state.items) == sum(len(i.result.mappings) for i in run["inputs"])
    _download(app, DRAFT_CSV_LABEL)


# ---------------------------------------------------------------------------
# Downloads + review state
# ---------------------------------------------------------------------------


def test_draft_downloads_present_after_crosswalk(at):
    _run_crosswalk(at)
    assert _download(at, DRAFT_CSV_LABEL).proto.disabled is False
    assert _download(at, DRAFT_XLSX_LABEL).proto.disabled is False


def test_accepted_downloads_disabled_until_one_accepted(at):

    _run_crosswalk(at)
    assert _download(at, ACCEPTED_CSV_LABEL).proto.disabled is True
    assert _download(at, ACCEPTED_XLSX_LABEL).proto.disabled is True
    _decide(at, FP_KEY, "Rejected", reason=REASON)
    _run(at)
    assert _download(at, ACCEPTED_CSV_LABEL).proto.disabled is True  # rejected != accepted
    _decide(at, DOWNGRADE_KEY, "Accepted")
    assert _download(at, ACCEPTED_CSV_LABEL).proto.disabled is False
    assert _download(at, ACCEPTED_XLSX_LABEL).proto.disabled is False


def test_review_state_created_all_pending_for_every_mapping(at):
    from control_mapper import review

    _run_crosswalk(at)
    assert SS_REVIEW in at.session_state
    state = at.session_state[SS_REVIEW]
    inputs = _raw_inputs(at)
    assert len(state.items) == sum(len(i.result.mappings) for i in inputs)
    assert all(it.status == review.ReviewStatus.PENDING for it in state.items.values())


def test_crosswalk_and_downloads_persist_across_rerun(at):
    _run_crosswalk(at, [NIST_L, ISO_L], anchor=NIST_L)
    before = _snap(_raw_inputs(at))
    _run(at)
    assert _snap(_raw_inputs(at)) == before
    _download(at, DRAFT_CSV_LABEL)
    _download(at, ACCEPTED_CSV_LABEL)


# ---------------------------------------------------------------------------
# Wiring: accepted pair gets exactly accepted_inputs(state, raw)
# ---------------------------------------------------------------------------


class _Spies:
    def __init__(self):
        self.build, self.csv, self.xlsx = [], [], []

    def clear(self):
        self.build.clear(), self.csv.clear(), self.xlsx.clear()

    @staticmethod
    def accepted(calls):
        return [c for c in calls if c.get("review") is not None]

    @staticmethod
    def draft(calls):
        return [c for c in calls if c.get("review") is None]


def _args(a, k):
    out = dict(zip(("crosswalk", "statements", "inputs", "review"), a))
    out.update(k)
    return out


@pytest.fixture
def spies(monkeypatch):
    import control_mapper
    import control_mapper.crosswalk as cwmod
    import control_mapper.export as exmod

    rec = _Spies()
    real_build = cwmod.build_crosswalk
    real_csv = getattr(exmod, "crosswalk_csv_bytes", None)
    real_xlsx = getattr(exmod, "crosswalk_xlsx_bytes", None)
    rec.real_build = real_build

    def build_spy(statements, inputs):
        rec.build.append(list(inputs))
        return real_build(statements, inputs)

    def csv_spy(*a, **k):
        rec.csv.append(_args(a, k))
        return real_csv(*a, **k) if real_csv else b""

    def xlsx_spy(*a, **k):
        rec.xlsx.append(_args(a, k))
        return real_xlsx(*a, **k) if real_xlsx else b""

    _sweep(monkeypatch, "build_crosswalk", real_build, build_spy, (control_mapper, cwmod))
    _sweep(monkeypatch, "crosswalk_csv_bytes", real_csv, csv_spy, (control_mapper, exmod))
    _sweep(monkeypatch, "crosswalk_xlsx_bytes", real_xlsx, xlsx_spy, (control_mapper, exmod))
    return rec


def test_accepted_downloads_fed_exactly_accepted_inputs(spies):
    from control_mapper import review
    from control_mapper.frameworks import parse_policy
    from control_mapper.models import CoverageLevel

    app = _new_app()
    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    state = app.session_state[SS_REVIEW]
    raw = _raw_inputs(app)
    # Setup by direct state mutation (not the behaviour under test; ~44 panel saves
    # would only slow this down). The panel itself is covered by the panel tests.
    for key in list(state.items):
        review.set_status(state, key, review.ReviewStatus.ACCEPTED)
    review.set_status(state, _sk(app, FP_KEY), review.ReviewStatus.REJECTED, reason=REASON)
    review.set_coverage(state, _sk(app, DOWNGRADE_KEY), CoverageLevel.PARTIAL, reason=REASON)
    spies.clear()
    _run(app)

    acc = review.accepted_inputs(state, raw)
    expected = _snap(acc)
    assert expected != _snap(raw)  # sanity: oracle differs from the raw proposals
    statements = parse_policy(_policy(app).value)
    expected_cw = spies.real_build(statements, acc).to_dict()

    for calls, name in ((spies.csv, "csv"), (spies.xlsx, "xlsx")):
        acc_calls = spies.accepted(calls)
        assert acc_calls, f"accepted {name} export never called with review=state"
        for c in acc_calls:
            assert c["review"] is state
            assert _snap(c["inputs"]) == expected, f"accepted {name} fed non-accepted mappings"
            assert c["crosswalk"].to_dict() == expected_cw
    assert any(_snap(b) == expected for b in spies.build), "accepted crosswalk never built"


def test_draft_downloads_fed_raw_inputs(spies):
    app = _new_app()
    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    raw = _snap(_raw_inputs(app))
    spies.clear()
    _run(app)
    for calls, name in ((spies.csv, "csv"), (spies.xlsx, "xlsx")):
        draft = spies.draft(calls)
        assert draft, f"draft {name} export never called"
        assert all(_snap(c["inputs"]) == raw for c in draft)


# ---------------------------------------------------------------------------
# Out of date: results and decisions kept, flagged
# ---------------------------------------------------------------------------


def _crosswalked_with_decision(app):

    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    _decide(app, FP_KEY, "Rejected", reason=REASON)
    _run(app)
    assert not _has_out_of_date(app), "fresh run must not be flagged out of date"
    return _snap(_raw_inputs(app))


def _assert_kept(app, before):
    from control_mapper import review

    assert _snap(_raw_inputs(app)) == before, "stale results must be kept, not recomputed"
    status = app.session_state[SS_REVIEW].items[_sk(app, FP_KEY)].status
    assert status == review.ReviewStatus.REJECTED, "decisions lost"


def test_policy_change_marks_crosswalk_out_of_date_and_keeps_it(at):
    before = _crosswalked_with_decision(at)
    _policy(at).set_value(NEW_POLICY)
    _run(at)
    assert _has_out_of_date(at)
    _assert_kept(at, before)


def test_framework_set_change_marks_crosswalk_out_of_date_and_keeps_it(at):
    before = _crosswalked_with_decision(at)
    _select_frameworks(at, [NIST_L, ISO_L])
    assert _has_out_of_date(at)
    _assert_kept(at, before)


def test_anchor_change_is_not_out_of_date_and_keeps_decisions(at):
    before = _crosswalked_with_decision(at)
    _xw_anchor(at).set_value(ISO_L)
    _run(at)
    assert not _has_out_of_date(at)
    _assert_kept(at, before)


# ---------------------------------------------------------------------------
# Re-run guard and filter persistence
# ---------------------------------------------------------------------------


def _all_text(app):
    out = []
    for kind in ("warning", "info", "success", "error", "markdown", "caption"):
        out += [str(e.value) for e in getattr(app, kind)]
    return out


def _confirm_counts(app):
    """(kept, reset) from the confirm message "N decisions kept, M reset to pending
    (statement changed)"."""
    for t in _all_text(app):
        m = CONFIRM_COUNTS_RE.search(t)
        if m:
            return int(m.group(1)), int(m.group(2))
    pytest.fail(f"confirm message with kept/reset counts not shown; text: {_all_text(app)}")


def _click_run_expect_confirm(app):
    _button(app, XW_RUN_LABEL).click()
    _run(app)
    assert _has_key(app.button, RERUN_CONFIRM_KEY), "no confirm step before re-run"
    assert app.button(key=RERUN_CONFIRM_KEY).label == CONFIRM_RUN_LABEL
    assert _keyed(app.button, RERUN_CANCEL_KEY, "button").label == CANCEL_LABEL
    return _confirm_counts(app)


def test_rerun_with_decisions_confirms_then_carries_decisions_over(at):
    from control_mapper import review

    _crosswalked_with_decision(at)
    assert _click_run_expect_confirm(at) == (1, 0)
    at.button(key=RERUN_CONFIRM_KEY).click()
    _run(at)
    assert not _has_key(at.button, RERUN_CONFIRM_KEY)
    items = at.session_state[SS_REVIEW].items
    assert items[_sk(at, FP_KEY)].status == review.ReviewStatus.REJECTED, "decision not carried"
    others = [it for k, it in items.items() if k != _sk(at, FP_KEY)]
    assert all(it.status == review.ReviewStatus.PENDING for it in others)


def test_rerun_confirm_cancel_does_not_rerun(at):
    from control_mapper import review

    before = _crosswalked_with_decision(at)
    _policy(at).set_value(NEW_POLICY)  # a real re-run would change the inputs
    _run(at)
    _click_run_expect_confirm(at)
    at.button(key=RERUN_CANCEL_KEY).click()
    _run(at)
    assert not _has_key(at.button, RERUN_CONFIRM_KEY)
    assert _snap(_raw_inputs(at)) == before, "Cancel must not re-run"
    assert _has_out_of_date(at)
    assert at.session_state[SS_REVIEW].items[_sk(at, FP_KEY)].status == review.ReviewStatus.REJECTED


def test_rerun_without_decisions_runs_without_confirm(at):
    _run_crosswalk(at, ALL_L, anchor=NIST_L)
    _button(at, XW_RUN_LABEL).click()
    _run(at)
    assert not _has_key(at.button, RERUN_CONFIRM_KEY)
    assert SS_RUN in at.session_state


# ---------------------------------------------------------------------------
# Policy name = stable policy_id; renumbering keeps decisions (ADR 0001 D2)
# ---------------------------------------------------------------------------


@pytest.fixture
def text_stub(monkeypatch):
    """Offline stand-in for a model that maps by statement TEXT: the real StubClient rows
    (written against the sample's P-labels) are re-labelled to whatever label the same
    text has in the prompt; rows whose text is absent are dropped."""
    from control_mapper.frameworks import parse_policy
    from eval import stub_client

    sample = {st.id: st.text for st in parse_policy(
        (ROOT / "data" / "sample_policies" / "sample_policy.md").read_text())}
    real = stub_client.StubClient.get_mappings

    def by_text(self, system, tool, user_content):
        block = user_content.split("POLICY STATEMENTS:", 1)[1]
        label_by_text = {}
        for line in block.splitlines():
            if line.startswith("- ") and ": " in line:
                lab, txt = line[2:].split(": ", 1)
                label_by_text[txt] = lab
        rows = []
        for row in real(self, system, tool, user_content):
            lab = label_by_text.get(sample.get(row["statement_id"], ""))
            if lab:
                rows.append({**row, "statement_id": lab})
        return rows

    monkeypatch.setattr(stub_client.StubClient, "get_mappings", by_text)


def _sample_statements():
    from control_mapper.frameworks import parse_policy

    return parse_policy((ROOT / "data" / "sample_policies" / "sample_policy.md").read_text())


def _policy_with_new_first_line(edit_label=None):
    lines = ["1. A brand new first obligation is inserted at the top."]
    for st in _sample_statements():
        text = st.text + (" Reviewed yearly." if st.id == edit_label else "")
        lines.append(f"{int(st.id[1:]) + 1}. {text}")
    return "\n".join(lines)


def test_policy_name_is_the_stable_policy_id(at):
    name = _keyed(at.text_input, POLICY_NAME_KEY, "text_input")
    assert name.value == POLICY_NAME_DEFAULT
    _run_crosswalk(at, ALL_L, anchor=NIST_L)
    state = at.session_state[SS_REVIEW]
    assert state.policy_id == name.value
    assert all(k[0] == name.value for k in state.items)


def _decided(app):

    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    _decide(app, FP_KEY, "Rejected", reason=REASON)
    _decide(app, DOWNGRADE_KEY, "Accepted")
    _run(app)


def test_insert_statement_at_top_rerun_confirm_keeps_shifted_decisions(text_stub):
    from control_mapper import review

    app = _new_app()
    _decided(app)
    _policy(app).set_value(_policy_with_new_first_line())
    _run(app)
    assert _click_run_expect_confirm(app) == (2, 0)
    app.button(key=RERUN_CONFIRM_KEY).click()
    _run(app)
    items = app.session_state[SS_REVIEW].items
    # Old P5 is now P6, old P2 is now P3: same text, same stable key, same decision.
    assert items[_sk(app, (NIST_ID, "P6", "SC-7"))].status == review.ReviewStatus.REJECTED
    assert items[_sk(app, (NIST_ID, "P3", "AC-2"))].status == review.ReviewStatus.ACCEPTED
    assert not _has_out_of_date(app)


def test_rerun_after_editing_a_statement_resets_only_that_decision(text_stub):
    from control_mapper import review

    app = _new_app()
    _decided(app)
    _policy(app).set_value(_policy_with_new_first_line(edit_label="P5"))
    _run(app)
    assert _click_run_expect_confirm(app) == (1, 1)
    app.button(key=RERUN_CONFIRM_KEY).click()
    _run(app)
    items = app.session_state[SS_REVIEW].items
    assert items[_sk(app, (NIST_ID, "P3", "AC-2"))].status == review.ReviewStatus.ACCEPTED
    assert all(it.status != review.ReviewStatus.REJECTED for it in items.values())


def test_decisions_survive_filter_change(at):
    from control_mapper import review

    _crosswalked_with_decision(at)
    _decide(at, DOWNGRADE_KEY, "Accepted")
    flt = _by_label(at.multiselect, REVIEW_DECISION_FILTER_LABEL, "multiselect")
    flt.set_value(["Pending"])
    _run(at)
    flt = _by_label(at.multiselect, REVIEW_DECISION_FILTER_LABEL, "multiselect")
    flt.set_value(["Pending", "Accepted", "Rejected"])
    _run(at)
    items = at.session_state[SS_REVIEW].items
    assert items[_sk(at, FP_KEY)].status == review.ReviewStatus.REJECTED
    assert items[_sk(at, DOWNGRADE_KEY)].status == review.ReviewStatus.ACCEPTED


# ---------------------------------------------------------------------------
# Decide panel (notes.md v2 §4 save rules) -- the ONLY write path
# ---------------------------------------------------------------------------


def _status(app, triple):
    return app.session_state[SS_REVIEW].items[_sk(app, triple)]


def _crosswalked(app):
    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    return app


def test_review_table_is_read_only_and_picker_uses_label_framework_control(at):
    _crosswalked(at)
    assert len(at.data_editor) == 0, "no st.data_editor: the panel is the only write path"
    pick = _keyed(at.selectbox, PICK_KEY, "selectbox")
    assert _pick_value(FP_KEY) in pick.options
    assert all(o.count("|") == 2 for o in pick.options)
    _keyed(at.button, SAVE_KEY, "button")


def test_reject_without_reason_shows_error_and_saves_nothing(at):
    from control_mapper import review

    _crosswalked(at)
    _decide(at, FP_KEY, "Rejected", reason="   ", expect="error")
    assert any(REASON_ERROR in str(e.value) for e in at.error)
    assert _status(at, FP_KEY).status == review.ReviewStatus.PENDING


def test_reject_with_reason_is_saved_with_reason(at):
    from control_mapper import review

    _crosswalked(at)
    _decide(at, FP_KEY, "Rejected", reason="network mention is not a boundary control")
    item = _status(at, FP_KEY)
    assert item.status == review.ReviewStatus.REJECTED
    assert item.reason == "network mention is not a boundary control"


def test_coverage_change_without_reason_shows_error_and_saves_nothing(at):
    from control_mapper import review
    from control_mapper.models import CoverageLevel

    _crosswalked(at)
    _decide(at, DOWNGRADE_KEY, "Accepted", coverage="partial", reason="", expect="error")
    assert any(REASON_ERROR in str(e.value) for e in at.error)
    item = _status(at, DOWNGRADE_KEY)
    assert item.status == review.ReviewStatus.PENDING  # nothing half-saved
    assert item.coverage == CoverageLevel.FULL


def test_coverage_change_with_reason_saves_both(at):
    from control_mapper import review
    from control_mapper.models import CoverageLevel

    _crosswalked(at)
    _decide(at, DOWNGRADE_KEY, "Accepted", coverage="partial", reason="quarterly review only")
    item = _status(at, DOWNGRADE_KEY)
    assert item.status == review.ReviewStatus.ACCEPTED
    assert item.coverage == CoverageLevel.PARTIAL
    assert item.coverage_reason == "quarterly review only"  # code-review defect 1


def test_accept_without_reason_is_saved(at):
    from control_mapper import review

    _crosswalked(at)
    _decide(at, DOWNGRADE_KEY, "Accepted")
    assert _status(at, DOWNGRADE_KEY).status == review.ReviewStatus.ACCEPTED


def test_unchanged_save_says_nothing_to_save(at):
    from control_mapper import review

    _crosswalked(at)
    _decide(at, DOWNGRADE_KEY, "Pending", reason="", expect="nothing")
    assert any(str(i.value).startswith(NOTHING_TO_SAVE) for i in at.info)
    assert _status(at, DOWNGRADE_KEY).status == review.ReviewStatus.PENDING


def test_coverage_select_disabled_unless_accepted(at):
    _crosswalked(at)
    _pick(at, DOWNGRADE_KEY)
    for decision, disabled in (("Rejected", True), ("Pending", True), ("Accepted", False)):
        _keyed(at.radio, DECISION_KEY, "radio").set_value(decision)
        _run(at)
        assert _keyed(at.selectbox, COVERAGE_KEY, "selectbox").proto.disabled is disabled, decision


def test_changing_picker_resets_typed_reason(at):
    from control_mapper import review

    _crosswalked(at)
    _pick(at, FP_KEY)
    _keyed(at.radio, DECISION_KEY, "radio").set_value("Rejected")
    _keyed(at.text_input, REASON_KEY, "text_input").input("meant for SC-7 only")
    _run(at)
    _pick(at, DOWNGRADE_KEY)
    assert _keyed(at.text_input, REASON_KEY, "text_input").value in ("", None)
    assert _keyed(at.radio, DECISION_KEY, "radio").value == "Pending"
    _keyed(at.button, SAVE_KEY, "button").click()
    _run(at)
    assert _status(at, DOWNGRADE_KEY).reason in (None, "")
    assert _status(at, FP_KEY).status == review.ReviewStatus.PENDING


def test_filter_change_resets_picker_to_view_default(at):
    _crosswalked(at)
    _decide(at, FP_KEY, "Rejected", reason=REASON)
    flt = _by_label(at.multiselect, REVIEW_DECISION_FILTER_LABEL, "multiselect")
    flt.set_value(["Rejected"])
    _run(at)
    pick = _keyed(at.selectbox, PICK_KEY, "selectbox")
    assert pick.options == [_pick_value(FP_KEY)]
    assert pick.value == _pick_value(FP_KEY)
    assert _keyed(at.radio, DECISION_KEY, "radio").value == "Rejected"
    flt = _by_label(at.multiselect, REVIEW_DECISION_FILTER_LABEL, "multiselect")
    flt.set_value(["Pending"])
    _run(at)
    pick = _keyed(at.selectbox, PICK_KEY, "selectbox")
    assert pick.value == pick.options[0] and pick.value != _pick_value(FP_KEY)
    assert _keyed(at.text_input, REASON_KEY, "text_input").value in ("", None)


# ---------------------------------------------------------------------------
# Policy name: required; renaming means nothing carries over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
def test_blank_policy_name_disables_run_crosswalk(at, blank):
    _keyed(at.text_input, POLICY_NAME_KEY, "text_input").input(blank)
    _run(at)
    _select_frameworks(at, ALL_L)
    assert _button(at, XW_RUN_LABEL).proto.disabled is True
    assert SS_RUN not in at.session_state


def test_policy_name_change_is_out_of_date(at):
    _crosswalked_with_decision(at)
    _keyed(at.text_input, POLICY_NAME_KEY, "text_input").input("Renamed policy")
    _run(at)
    assert _has_out_of_date(at)


def test_renamed_policy_rerun_reports_and_carries_nothing(at):
    from control_mapper import review

    _crosswalked_with_decision(at)  # 1 decision (FP rejected)
    _keyed(at.text_input, POLICY_NAME_KEY, "text_input").input("Renamed policy")
    _run(at)
    _button(at, XW_RUN_LABEL).click()
    _run(at)
    assert _has_key(at.button, RERUN_CONFIRM_KEY)
    counts = [CONFIRM_RENAMED_RE.search(t) for t in _all_text(at)]
    found = [m for m in counts if m]
    assert found, f"renamed-policy confirm copy missing: {_all_text(at)}"
    assert (int(found[0].group(1)), int(found[0].group(2))) == (0, 1)
    at.button(key=RERUN_CONFIRM_KEY).click()
    _run(at)
    state = at.session_state[SS_REVIEW]
    assert state.policy_id == "Renamed policy"
    assert all(it.status == review.ReviewStatus.PENDING for it in state.items.values())


# ---------------------------------------------------------------------------
# Code-review defects (25 Nov): red until fixed
# ---------------------------------------------------------------------------

RESTARTED_NUMBERING = "1. Reviewed annually.\n2. Logs are kept.\n1. MFA for remote access.\n"


def _errors(app):
    return [str(e.value) for e in app.error]


def _has_error(app, needle):
    return any(needle in e.lower() for e in _errors(app))


def test_map_with_duplicate_statement_numbers_shows_error_not_results(at):
    """Defect 2: restarted numbering (P1, P2, P1) must not crash or mis-assign."""
    _policy(at).set_value(RESTARTED_NUMBERING)
    _run(at)
    _button(at, MAP_BUTTON).click()
    _run(at)  # asserts no exception
    assert _has_error(at, DUP_LABEL_ERROR), _errors(at)
    assert SS_MAP not in at.session_state, "mapped despite duplicate statement numbers"
    assert MAP_METRIC not in _metric_labels(at)


def test_crosswalk_with_duplicate_statement_numbers_shows_error_not_results(at):
    _policy(at).set_value(RESTARTED_NUMBERING)
    _run(at)
    _run_crosswalk(at, ALL_L)  # asserts no exception
    assert _has_error(at, DUP_LABEL_ERROR), _errors(at)
    assert SS_RUN not in at.session_state, "crosswalk stored despite duplicate numbers"
    assert SS_REVIEW not in at.session_state, "review state built on ambiguous labels"


BULK_RE = __import__("re").compile(
    r"(?i)\bbulk\b|\b(accept|approve|reject|decide|select|mark)\s+(all|every|selected|visible)\b"
)


def test_no_bulk_accept_anywhere_in_the_ui(at):
    """Defect 3 (pin): one mapping at a time, through the Decide panel only."""
    _crosswalked(at)
    labels = [w.label for kind in ("button", "checkbox", "toggle", "radio", "selectbox",
                                   "multiselect", "download_button")
              for w in getattr(at, kind)]
    options = [str(o) for kind in ("radio", "selectbox", "multiselect")
               for w in getattr(at, kind) for o in (w.options or [])]
    hits = [t for t in labels + options if BULK_RE.search(t or "")]
    assert hits == [], f"bulk-decision widget(s): {hits}"
    assert len(at.data_editor) == 0
    assert "data_editor" not in Path(APP).read_text(encoding="utf-8")


def test_bulk_regex_detects_planted_labels():
    """Harness self-test (passes today): proves the bulk gate can fail."""
    for planted in ("Accept all", "Bulk accept", "Select all", "approve selected"):
        assert BULK_RE.search(planted), planted
    for fine in ("Save decision", "Accepted", "Download accepted CSV", "Decision"):
        assert not BULK_RE.search(fine), fine


@pytest.fixture
def server_address():
    """Set streamlit's server.address for one test, then restore it."""
    from streamlit import config as st_config

    old = st_config.get_option("server.address")

    def _set(value):
        st_config.set_option("server.address", value)

    yield _set
    st_config.set_option("server.address", old)


@pytest.mark.parametrize("address", [None, "", "0.0.0.0", "::", "192.168.1.10",  # noqa: S104
                                     "example.com", "localhost.evil.example"],
                         ids=["unset", "empty", "any-v4", "any-v6", "lan", "host",
                              "localhost-suffix"])
def test_non_loopback_address_stops_before_rendering_data(server_address, address):
    """Defect 7: startup guard -- a non-loopback bind shows an error and st.stop()s."""
    server_address(address)
    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert _has_error(app, NON_LOOPBACK_ERROR), _errors(app)
    assert len(app.tabs) == 0, "tabs rendered despite non-loopback bind"
    assert len(app.text_area) == 0, "policy text rendered despite non-loopback bind"
    assert len(app.dataframe) == 0 and len(app.button) == 0


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "localhost"])
def test_loopback_address_renders_normally(server_address, address):
    server_address(address)
    app = _new_app()
    assert not _has_error(app, NON_LOOPBACK_ERROR), _errors(app)
    assert len(app.tabs) >= len(TABS)


COVERAGE_REASON_LABEL = "Coverage reason"


def test_review_panel_shows_coverage_reason_as_plain_text(at):
    """The Decide panel shows a "Coverage reason" line; the value goes through st.text."""
    _crosswalked(at)
    _decide(at, DOWNGRADE_KEY, "Accepted", coverage="partial", reason="quarterly review only")
    _pick(at, DOWNGRADE_KEY)
    labels = [str(e.value) for e in at.markdown] + [str(e.value) for e in at.text]
    assert any(COVERAGE_REASON_LABEL in t for t in labels), "no 'Coverage reason' line"
    texts = [str(e.value) for e in at.text]
    assert any("quarterly review only" in t for t in texts), texts
    assert not any("quarterly review only" in str(e.value) for e in at.markdown), (
        "coverage reason rendered as markdown")
