"""Red tests for human review of mappings + in-memory exports (pure logic, no Streamlit).

CONTRACT for the developer (keep it minimal; built on models.py / crosswalk.py / export.py)
=========================================================================================

src/control_mapper/review.py  (new, pure -- no Streamlit, no I/O)

    class ReviewStatus(str, Enum):
        PENDING = "pending"; ACCEPTED = "accepted"; REJECTED = "rejected"

    # Stable identity per docs/adr/0001 D2/D11 -- never the positional label "P3".
    StatementKey = Tuple[str, str, int]        # (policy_id, statement_hash, occurrence)
    MappingKey = Tuple[str, str, int, str, str]
        # (policy_id, statement_hash, occurrence, framework_id, control_id)

    def normalise_statement(text: str) -> str  # " ".join(text.split())  (whitespace only)
    def statement_hash(text: str) -> str       # sha256(normalise_statement(text)) hexdigest
    def statement_keys(policy_id: str, statements: List[PolicyStatement])
            -> Dict[str, StatementKey]         # display label ("P3") -> stable key;
                                               # occurrence = 1-based ordinal among
                                               # statements with identical normalised text
                                               # ValueError if two statements share a label
                                               # (e.g. numbering restarts: P1, P2, P1);
                                               # start_review raises the same.

    @dataclass
    class ReviewItem:
        framework_id: str
        mapping: Mapping                       # the ORIGINAL model proposal, never mutated
        coverage: CoverageLevel                # reviewer's coverage; starts = mapping.coverage
        status: ReviewStatus = ReviewStatus.PENDING
        actor: Optional[str] = None            # who made the last change   (D11; S1 makes
        reason: Optional[str] = None           # why                         these required)
        at: Optional[datetime] = None          # when -- caller-supplied (injectable clock);
                                               # review.py never calls datetime.now()
        coverage_reason: Optional[str] = None  # why the coverage was changed; written ONLY
                                               # by set_coverage, never cleared by
                                               # set_status; carried over by start_review

    @dataclass
    class ReviewState:
        policy_id: str
        items: Dict[MappingKey, ReviewItem]
        # plus whatever it needs to translate this run's labels (statement_keys)

    def start_review(policy_id: str, statements: List[PolicyStatement],
                     inputs: List[CrosswalkInput],
                     previous: Optional[ReviewState] = None) -> ReviewState
        # one item per mapping across all frameworks, PENDING -- except that a key also
        # present in `previous` carries over its status, coverage, actor, reason, at (renumbering the
        # policy must not lose decisions; edited text = new hash = PENDING, per D2).
    def key_for(state: ReviewState, framework_id: str, statement_label: str,
                control_id: str) -> MappingKey  # label of THIS run -> stable key; KeyError
    def set_status(state: ReviewState, key: MappingKey, status: ReviewStatus, *,
                   actor: Optional[str] = None, reason: Optional[str] = None,
                   at: Optional[datetime] = None) -> None
        # KeyError for an unknown key; any status -> any status is allowed
        # actor/reason/at are keyword-only, recorded on the item as given.
        # REASON RULE (owner-confirmed): status REJECTED requires a reason that is not
        # None/empty/whitespace-only -> ValueError otherwise, state unchanged. ACCEPTED
        # needs no reason. actor and at stay optional.
    def set_coverage(state: ReviewState, key: MappingKey, coverage: CoverageLevel, *,
                     actor: Optional[str] = None, reason: Optional[str] = None,
                     at: Optional[datetime] = None) -> None
        # only FULL / PARTIAL; CoverageLevel.NONE -> ValueError (reject instead); KeyError unknown
        # a non-blank reason is required when `coverage` differs from the item's CURRENT
        # stored coverage (not the AI coverage) -> ValueError otherwise. Setting the same
        # coverage again is a no-op WITH OR WITHOUT a reason (ADR 0001 S1). A real change
        # writes ONLY coverage, coverage_reason (trimmed), actor and at; set_coverage
        # never writes item.reason (that is the decision reason, set_status's field).
    def accepted_inputs(state: ReviewState, inputs: List[CrosswalkInput]) -> List[CrosswalkInput]
        # same frameworks, same order; ONLY accepted mappings, with the reviewer's coverage
        # applied; returns new objects -- `inputs` must not be mutated.
        # The crosswalk and every export are rebuilt from this, so rejected/pending
        # mappings never reach a download.

src/control_mapper/export.py  (add; in memory, no disk I/O; per review-ui-mockups/notes.md §6)

    ACCEPTED_CSV_HEADER: List[str] containing at least ACCEPTED_REQUIRED_COLUMNS below
        (notes.md v2 §6 + main session: reviewed_at, actor). Values per row:
        policy_name = state.policy_id; decision = "accepted"; coverage = reviewer's;
        ai_coverage = model's; reason / actor = as stored, "" when None;
        reviewed_at = item.at ISO 8601 (datetime.isoformat()), "" when None;
        source_url = the framework JSON's source_url (never model/user text).
    def crosswalk_csv_bytes(crosswalk, statements, inputs, review=None) -> bytes
    def crosswalk_xlsx_bytes(crosswalk, statements, inputs, review=None) -> bytes
        review=None  -> DRAFT export: byte/cell-identical to crosswalk_to_csv / _xlsx.
        review=state -> ACCEPTED export. Caller passes inputs = accepted_inputs(state, raw)
                        Any mapping in `inputs` whose item is not ACCEPTED (e.g. raw
                        inputs passed by mistake) -> ValueError, nothing returned.
                        Column coverage_reason (item.coverage_reason or "") sits right
                        after reason.
                        and crosswalk = build_crosswalk(statements, those inputs).
                        CSV and Mappings sheet use ACCEPTED_CSV_HEADER (coverage = the
                        reviewer's, ai_coverage = the model's, decision = "accepted").
                        Summary text: "Reviewed: accepted mappings only (N of M). Pending
                        and rejected mappings are excluded." (N accepted, M all mappings),
                        plus "Policy: {policy_name}" and "Pending mappings: N (not counted
                        as coverage)". Crosswalk-sheet cell for a statement/framework with
                        only PENDING mappings starts "Pending review"; one with none reads
                        "No control mapped".

notes.md v2 §5 adopts this ReviewState contract.

Imports of not-yet-existing names are done inside each test so every test fails
individually (ImportError / ModuleNotFoundError) rather than one collection error.
Offline only: StubClient, never the live model.
"""

from __future__ import annotations

import builtins
import copy
import csv
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402,F401  (imported before any no-disk guard is installed)
from openpyxl import load_workbook  # noqa: E402

from control_mapper.crosswalk import CrosswalkInput, build_crosswalk  # noqa: E402
from control_mapper.export import crosswalk_to_csv, crosswalk_to_xlsx  # noqa: E402
from control_mapper.frameworks import load_framework, parse_policy  # noqa: E402
from control_mapper.mapper import ControlMapper  # noqa: E402
from control_mapper.models import CoverageLevel  # noqa: E402
from eval.stub_client import StubClient  # noqa: E402

FW_DIR = ROOT / "data" / "frameworks"
NIST = load_framework(FW_DIR / "nist_800-53_subset.json")
ISO = load_framework(FW_DIR / "iso_27001_2022_annexa_subset.json")
SOC2 = load_framework(FW_DIR / "soc2_tsc_subset.json")
ALL_FRAMEWORKS = [NIST, ISO, SOC2]
POLICY = (ROOT / "data" / "sample_policies" / "sample_policy.md").read_text()
POLICY_ID = "sample-policy"
REASON = "test: reviewer reason"  # REJECTED and coverage edits require one
NIST_ID = NIST.framework_id

# The stub's deliberate NIST false positive -- the realistic thing a reviewer rejects.
FALSE_POSITIVE = (NIST_ID, "P5", "SC-7")
# A full mapping a reviewer might downgrade to partial.
DOWNGRADE = (NIST_ID, "P2", "AC-2")


@pytest.fixture
def statements():
    return parse_policy(POLICY)


@pytest.fixture
def inputs(statements):
    return [
        CrosswalkInput(fw, ControlMapper(StubClient()).map(statements, fw))
        for fw in ALL_FRAMEWORKS
    ]


def _start(review, inputs, statements=None, previous=None, policy_id=POLICY_ID):
    statements = statements if statements is not None else parse_policy(POLICY)
    return review.start_review(policy_id, statements, inputs, previous=previous)


def _k(review, state, triple):
    """(framework_id, label, control_id) of this run -> stable MappingKey."""
    fid, label, cid = triple
    return review.key_for(state, fid, label, cid)


def _total_mappings(inputs):
    return sum(len(i.result.mappings) for i in inputs)


def _snapshot(inputs):
    return [(i.framework.framework_id, i.result.model_dump()) for i in inputs]


def _accept_all(state, review):
    for key in list(state.items):
        review.set_status(state, key, review.ReviewStatus.ACCEPTED)


def _csv_rows(data: bytes):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _sheet_values(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


# ---------------------------------------------------------------------------
# review.py -- state model
# ---------------------------------------------------------------------------


def test_start_review_creates_one_pending_item_per_stable_key(statements, inputs):
    from control_mapper import review

    state = _start(review, inputs, statements)
    assert state.policy_id == POLICY_ID
    # Equal counts prove keys never collide.
    assert len(state.items) == _total_mappings(inputs)
    skeys = review.statement_keys(POLICY_ID, statements)
    for inp in inputs:
        fid = inp.framework.framework_id
        for m in inp.result.mappings:
            key = review.key_for(state, fid, m.statement_id, m.control_id)
            assert key == (*skeys[m.statement_id], fid, m.control_id)
            assert m.statement_id not in key  # display label is not identity
            item = state.items[key]
            assert item.status == review.ReviewStatus.PENDING
            assert item.coverage == m.coverage
            assert item.framework_id == fid


def test_status_values_are_the_three_documented_strings():
    from control_mapper import review

    assert {s.value for s in review.ReviewStatus} == {"pending", "accepted", "rejected"}


def test_status_transitions_accept_reject_accept(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    key = _k(review, state, FALSE_POSITIVE)
    S = review.ReviewStatus
    for target in (S.ACCEPTED, S.REJECTED, S.ACCEPTED, S.PENDING):
        review.set_status(state, key, target, reason=REASON)
        assert state.items[key].status == target
    # Only the targeted item changed.
    others = [it for k, it in state.items.items() if k != key]
    assert all(it.status == S.PENDING for it in others)


def test_set_status_unknown_key_raises_keyerror(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    with pytest.raises(KeyError):
        review.key_for(state, NIST_ID, "P99", "AT-2")
    with pytest.raises(KeyError):
        review.key_for(state, "NOPE", "P1", "AT-2")
    bogus = (POLICY_ID, "0" * 64, 1, NIST_ID, "AT-2")
    with pytest.raises(KeyError):
        review.set_status(state, bogus, review.ReviewStatus.ACCEPTED)


def test_set_coverage_edits_and_rejects_none(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    review.set_coverage(state, _k(review, state, DOWNGRADE), CoverageLevel.PARTIAL, reason=REASON)
    assert state.items[_k(review, state, DOWNGRADE)].coverage == CoverageLevel.PARTIAL
    # Original proposal is preserved for audit.
    assert state.items[_k(review, state, DOWNGRADE)].mapping.coverage == CoverageLevel.FULL
    with pytest.raises(ValueError):
        review.set_coverage(state, _k(review, state, DOWNGRADE), CoverageLevel.NONE, reason=REASON)
    assert state.items[_k(review, state, DOWNGRADE)].coverage == CoverageLevel.PARTIAL
    with pytest.raises(KeyError):
        review.set_coverage(state, (POLICY_ID, "0" * 64, 1, NIST_ID, "AT-2"),
                            CoverageLevel.FULL, reason=REASON)


# ---------------------------------------------------------------------------
# review.py -- stable keys (ADR 0001 D2 / D11)
# ---------------------------------------------------------------------------


def test_statement_hash_is_sha256_of_whitespace_normalised_text():
    import hashlib

    from control_mapper import review

    text = "  Access   is granted\n on a  least-privilege basis. "
    norm = "Access is granted on a least-privilege basis."
    assert review.normalise_statement(text) == norm
    assert review.statement_hash(text) == hashlib.sha256(norm.encode("utf-8")).hexdigest()
    assert review.statement_hash(text) == review.statement_hash(norm)
    assert review.statement_hash(norm) != review.statement_hash(norm.lower())


def test_duplicate_statement_text_gets_distinct_occurrences():
    from control_mapper import review

    stmts = parse_policy("1. Reviewed annually.\n2. Logs are kept.\n3. Reviewed  annually.\n")
    keys = review.statement_keys(POLICY_ID, stmts)
    assert keys["P1"][:2] == keys["P3"][:2]          # same policy + hash
    assert (keys["P1"][2], keys["P3"][2]) == (1, 2)  # occurrence disambiguates
    assert keys["P2"][2] == 1
    assert len(set(keys.values())) == 3


def _relabel(inputs, mapping):
    """Same model output, statement labels renamed (what the model returns for the same
    text after the policy is renumbered)."""
    out = []
    for inp in inputs:
        ms = [m.model_copy(update={"statement_id": mapping[m.statement_id]})
              for m in inp.result.mappings]
        out.append(CrosswalkInput(inp.framework,
                                  inp.result.model_copy(update={"mappings": ms})))
    return out


def test_inserting_statement_at_top_keeps_decisions_on_the_others(statements, inputs):
    from control_mapper import review

    S = review.ReviewStatus
    old = _start(review, inputs, statements)
    review.set_status(old, _k(review, old, FALSE_POSITIVE), S.REJECTED, reason=REASON)
    review.set_status(old, _k(review, old, DOWNGRADE), S.ACCEPTED)
    review.set_coverage(old, _k(review, old, DOWNGRADE), CoverageLevel.PARTIAL, reason=REASON)

    new_policy = "1. A brand new first obligation is inserted here.\n" + "\n".join(
        f"{int(s.id[1:]) + 1}. {s.text}" for s in statements)
    new_statements = parse_policy(new_policy)
    assert new_statements[5].text == statements[4].text  # old P5 is now P6
    shift = {s.id: f"P{int(s.id[1:]) + 1}" for s in statements}
    new_inputs = _relabel(inputs, shift)

    new = _start(review, new_inputs, new_statements, previous=old)
    fp_new = review.key_for(new, NIST_ID, "P6", "SC-7")
    dg_new = review.key_for(new, NIST_ID, "P3", "AC-2")
    assert fp_new == _k(review, old, FALSE_POSITIVE)   # same stable key across labels
    assert new.items[fp_new].status == S.REJECTED
    assert new.items[dg_new].status == S.ACCEPTED
    assert new.items[dg_new].coverage == CoverageLevel.PARTIAL
    others = [it for k, it in new.items.items() if k not in (fp_new, dg_new)]
    assert all(it.status == S.PENDING for it in others)


def test_edited_statement_text_starts_pending(statements, inputs):
    from control_mapper import review

    S = review.ReviewStatus
    old = _start(review, inputs, statements)
    for key in list(old.items):
        review.set_status(old, key, S.ACCEPTED)
    edited = [s.model_copy(update={"text": s.text + " Reviewed yearly."})
              if s.id == "P5" else s for s in statements]
    new = _start(review, inputs, edited, previous=old)
    for key, it in new.items.items():
        want = S.PENDING if it.mapping.statement_id == "P5" else S.ACCEPTED
        assert it.status == want, (key, it.mapping.statement_id)


def test_decisions_do_not_carry_across_policies(statements, inputs):
    from control_mapper import review

    old = _start(review, inputs, statements)
    for key in list(old.items):
        review.set_status(old, key, review.ReviewStatus.ACCEPTED)
    other = _start(review, inputs, statements, previous=old, policy_id="other-policy")
    assert all(k[0] == "other-policy" for k in other.items)
    assert all(it.status == review.ReviewStatus.PENDING for it in other.items.values())


def test_actor_reason_at_are_recorded_and_optional(inputs):
    from datetime import datetime, timezone

    from control_mapper import review

    S = review.ReviewStatus
    state = _start(review, inputs)
    fp = _k(review, state, FALSE_POSITIVE)
    dg = _k(review, state, DOWNGRADE)
    t1 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 24, 9, 5, tzinfo=timezone.utc)

    item = state.items[fp]
    assert (item.actor, item.reason, item.at) == (None, None, None)
    review.set_status(state, fp, S.REJECTED, actor="analyst", reason="network != boundary",
                      at=t1)
    assert (item.actor, item.reason, item.at) == ("analyst", "network != boundary", t1)
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, actor="analyst",
                        reason="review cadence only", at=t2)
    d = state.items[dg]
    assert (d.actor, d.coverage_reason, d.at) == ("analyst", "review cadence only", t2)
    assert d.reason is None  # set_coverage never writes the decision reason
    # Optional in phase 1: omitting them still works.
    review.set_status(state, dg, S.ACCEPTED)
    assert state.items[dg].status == S.ACCEPTED


def test_actor_reason_at_are_keyword_only(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    fp = _k(review, state, FALSE_POSITIVE)
    with pytest.raises(TypeError):
        review.set_status(state, fp, review.ReviewStatus.REJECTED, "analyst")
    with pytest.raises(TypeError):
        review.set_coverage(state, fp, CoverageLevel.PARTIAL, "analyst")


def test_carry_over_keeps_actor_reason_at(statements, inputs):
    from datetime import datetime, timezone

    from control_mapper import review

    t1 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    old = _start(review, inputs, statements)
    fp = _k(review, old, FALSE_POSITIVE)
    review.set_status(old, fp, review.ReviewStatus.REJECTED, actor="a", reason="r", at=t1)
    new = _start(review, inputs, statements, previous=old)
    it = new.items[fp]
    assert (it.status, it.actor, it.reason, it.at) == (
        review.ReviewStatus.REJECTED, "a", "r", t1)


BLANK_REASONS = [None, "", " ", "\t\n "]
ACCEPTED_REQUIRED_COLUMNS = [
    "policy_name", "statement_id", "statement_text", "framework_id", "control_id",
    "control_title", "decision", "coverage", "ai_coverage", "confidence", "reason",
    "coverage_reason", "rationale", "source_url", "reviewed_at", "actor",
]
SEED_AT = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
LATER_AT = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _seed(review, state, key):
    """Give an item non-default actor/reason/at so 'unchanged' is a real check."""
    review.set_status(state, key, review.ReviewStatus.ACCEPTED, actor="seed-actor",
                      reason="seed reason", at=SEED_AT)
    return copy.deepcopy(state.items[key])


@pytest.mark.parametrize("bad", BLANK_REASONS, ids=["none", "empty", "space", "ws"])
def test_reject_requires_non_blank_reason(inputs, bad):
    from control_mapper import review

    state = _start(review, inputs)
    fp = _k(review, state, FALSE_POSITIVE)
    before = _seed(review, state, fp)
    kwargs = {} if bad is None else {"reason": bad}
    with pytest.raises(ValueError):
        review.set_status(state, fp, review.ReviewStatus.REJECTED, actor="other",
                          at=LATER_AT, **kwargs)
    # Whole item unchanged: status, coverage, actor, reason, at (and any later field).
    assert state.items[fp] == before
    assert (state.items[fp].actor, state.items[fp].reason, state.items[fp].at) == (
        "seed-actor", "seed reason", SEED_AT)


@pytest.mark.parametrize("bad", BLANK_REASONS, ids=["none", "empty", "space", "ws"])
def test_coverage_edit_requires_non_blank_reason(inputs, bad):
    from control_mapper import review

    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)
    before = _seed(review, state, dg)
    kwargs = {} if bad is None else {"reason": bad}
    with pytest.raises(ValueError):
        review.set_coverage(state, dg, CoverageLevel.PARTIAL, actor="other",
                            at=LATER_AT, **kwargs)
    assert state.items[dg] == before
    assert state.items[dg].coverage == CoverageLevel.FULL
    assert (state.items[dg].actor, state.items[dg].reason, state.items[dg].at) == (
        "seed-actor", "seed reason", SEED_AT)


def test_coverage_none_error_leaves_item_unchanged(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)
    before = _seed(review, state, dg)
    with pytest.raises(ValueError):
        review.set_coverage(state, dg, CoverageLevel.NONE, actor="other", reason=REASON,
                            at=LATER_AT)
    assert state.items[dg] == before



def test_coverage_reason_is_judged_against_current_not_ai_coverage(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)                      # AI coverage = full
    review.set_coverage(state, dg, CoverageLevel.FULL)     # same as current: no reason
    assert state.items[dg].coverage == CoverageLevel.FULL
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, reason="cadence only")
    review.set_coverage(state, dg, CoverageLevel.PARTIAL)  # same as current: no reason
    with pytest.raises(ValueError):                        # back to the AI value: differs
        review.set_coverage(state, dg, CoverageLevel.FULL)  # from current -> reason needed
    assert state.items[dg].coverage == CoverageLevel.PARTIAL


def test_accept_needs_no_reason_and_one_char_reason_is_enough(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    fp = _k(review, state, FALSE_POSITIVE)
    review.set_status(state, fp, review.ReviewStatus.ACCEPTED)
    assert state.items[fp].status == review.ReviewStatus.ACCEPTED
    review.set_status(state, fp, review.ReviewStatus.REJECTED, reason="x")
    assert state.items[fp].status == review.ReviewStatus.REJECTED
    review.set_coverage(state, fp, CoverageLevel.FULL, reason="x")
    assert state.items[fp].coverage == CoverageLevel.FULL


# ---------------------------------------------------------------------------
# review.py -- accepted-only derivation
# ---------------------------------------------------------------------------


def test_all_pending_yields_no_mappings(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    out = review.accepted_inputs(state, inputs)
    assert [i.framework.framework_id for i in out] == [f.framework_id for f in ALL_FRAMEWORKS]
    assert all(i.result.mappings == [] for i in out)


def test_accepted_inputs_keeps_only_accepted_and_applies_coverage(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    _accept_all(state, review)
    review.set_status(state, _k(review, state, FALSE_POSITIVE), review.ReviewStatus.REJECTED, reason=REASON)
    review.set_coverage(state, _k(review, state, DOWNGRADE), CoverageLevel.PARTIAL, reason=REASON)
    m0 = inputs[1].result.mappings[0]
    pending_key = review.key_for(state, ISO.framework_id, m0.statement_id, m0.control_id)
    review.set_status(state, pending_key, review.ReviewStatus.PENDING)

    out = review.accepted_inputs(state, inputs)
    got = {
        (i.framework.framework_id, m.statement_id, m.control_id): m.coverage
        for i in out
        for m in i.result.mappings
    }
    assert FALSE_POSITIVE not in got
    assert pending_key not in got
    assert got[DOWNGRADE] == CoverageLevel.PARTIAL
    assert len(got) == _total_mappings(inputs) - 2
    # framework_id on each result is preserved
    assert [i.result.framework_id for i in out] == [f.framework_id for f in ALL_FRAMEWORKS]


def test_accepted_inputs_does_not_mutate_original_inputs(inputs):
    from control_mapper import review

    before = _snapshot(inputs)
    state = _start(review, inputs)
    _accept_all(state, review)
    review.set_status(state, _k(review, state, FALSE_POSITIVE), review.ReviewStatus.REJECTED, reason=REASON)
    review.set_coverage(state, _k(review, state, DOWNGRADE), CoverageLevel.PARTIAL, reason=REASON)
    review.accepted_inputs(state, inputs)
    assert _snapshot(inputs) == before


def test_crosswalk_rebuilt_from_accepted_only(statements, inputs):
    from control_mapper import review

    state = _start(review, inputs)
    _accept_all(state, review)
    review.set_status(state, _k(review, state, FALSE_POSITIVE), review.ReviewStatus.REJECTED, reason=REASON)
    review.set_coverage(state, _k(review, state, DOWNGRADE), CoverageLevel.PARTIAL, reason=REASON)
    cw = build_crosswalk(statements, review.accepted_inputs(state, inputs))
    p5 = next(r for r in cw.rows if r.statement_id == "P5")
    p2 = next(r for r in cw.rows if r.statement_id == "P2")
    assert "SC-7" not in [c for c, _ in p5.by_framework[NIST_ID]]
    assert ("AC-2", "partial") in p2.by_framework[NIST_ID]


# ---------------------------------------------------------------------------
# export.py -- in-memory bytes, identical to the path-based writers
# ---------------------------------------------------------------------------


@pytest.fixture
def no_disk(tmp_path, monkeypatch):
    """Fail loudly on any temp-file or write-mode open; run in an empty cwd."""
    work = tmp_path / "cwd"
    work.mkdir()
    sentinel = tmp_path / "tempdir-sentinel"
    sentinel.mkdir()

    def _boom(*a, **k):
        raise AssertionError("export touched disk via tempfile")

    for name in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile",
                 "TemporaryDirectory", "SpooledTemporaryFile"):
        monkeypatch.setattr(tempfile, name, _boom)
    monkeypatch.setattr(tempfile, "tempdir", str(sentinel))

    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)) and any(c in mode for c in "wax+"):
            raise AssertionError(f"export opened {file!r} for writing")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)  # zipfile/openpyxl use io.open
    monkeypatch.chdir(work)
    yield
    assert list(work.iterdir()) == []
    assert list(sentinel.iterdir()) == []


def test_no_disk_guard_catches_the_path_writers(tmp_path, statements, inputs, request):
    """Harness self-test (passes today): proves the no-disk gate can fail."""
    cw = build_crosswalk(statements, inputs)
    outside = tmp_path / "outside"
    outside.mkdir()
    request.getfixturevalue("no_disk")
    with pytest.raises(AssertionError, match="for writing"):
        crosswalk_to_csv(cw, statements, inputs, outside / "x.csv")
    with pytest.raises(AssertionError, match="for writing"):
        crosswalk_to_xlsx(cw, statements, inputs, outside / "x.xlsx")
    assert list(outside.iterdir()) == []


def _oracle_files(tmp_path, statements, inputs):
    cw = build_crosswalk(statements, inputs)
    csv_path = tmp_path / "oracle.csv"
    xlsx_path = tmp_path / "oracle.xlsx"
    crosswalk_to_csv(cw, statements, inputs, csv_path)
    crosswalk_to_xlsx(cw, statements, inputs, xlsx_path)
    return cw, csv_path, xlsx_path


def test_csv_bytes_matches_file_export_and_parses(tmp_path, statements, inputs, request):
    from control_mapper.export import CSV_HEADER, crosswalk_csv_bytes

    cw, csv_path, _ = _oracle_files(tmp_path, statements, inputs)
    expected = csv_path.read_bytes().decode("utf-8-sig")
    request.getfixturevalue("no_disk")
    data = crosswalk_csv_bytes(cw, statements, inputs)
    assert isinstance(data, bytes)
    assert data.decode("utf-8-sig") == expected
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
    assert next(reader) == CSV_HEADER
    rows = _csv_rows(data)
    assert len(rows) == _total_mappings(inputs)
    assert {r["framework_id"] for r in rows} == {f.framework_id for f in ALL_FRAMEWORKS}


def test_xlsx_bytes_matches_file_export_and_opens(tmp_path, statements, inputs, request):
    from control_mapper.export import crosswalk_xlsx_bytes

    cw, _, xlsx_path = _oracle_files(tmp_path, statements, inputs)
    oracle = load_workbook(xlsx_path)
    request.getfixturevalue("no_disk")
    data = crosswalk_xlsx_bytes(cw, statements, inputs)
    assert isinstance(data, bytes) and data[:2] == b"PK"
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == oracle.sheetnames
    assert set(wb.sheetnames) == {"Summary", "Crosswalk", "Mappings", "Gaps"}
    for name in wb.sheetnames:
        assert _sheet_values(wb[name]) == _sheet_values(oracle[name]), name


# ---------------------------------------------------------------------------
# Accepted export (review=state): accepted only, extra columns, own Summary wording
# ---------------------------------------------------------------------------


def _reviewed_state(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    _accept_all(state, review)
    review.set_status(state, _k(review, state, FALSE_POSITIVE), review.ReviewStatus.REJECTED, reason=REASON)
    review.set_coverage(state, _k(review, state, DOWNGRADE), CoverageLevel.PARTIAL, reason=REASON)
    return state


def _accepted_exports(statements, inputs, state):
    from control_mapper import review
    from control_mapper.export import crosswalk_csv_bytes, crosswalk_xlsx_bytes

    acc = review.accepted_inputs(state, inputs)
    cw = build_crosswalk(statements, acc)
    return (
        crosswalk_csv_bytes(cw, statements, acc, review=state),
        load_workbook(io.BytesIO(crosswalk_xlsx_bytes(cw, statements, acc, review=state))),
    )


def test_accepted_csv_has_required_columns_and_values(statements, inputs):
    from datetime import datetime, timezone

    from control_mapper import review
    from control_mapper.export import ACCEPTED_CSV_HEADER

    assert set(ACCEPTED_REQUIRED_COLUMNS) <= set(ACCEPTED_CSV_HEADER)
    t1 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    state = _reviewed_state(inputs)
    dg = _k(review, state, DOWNGRADE)
    review.set_coverage(state, dg, CoverageLevel.FULL, reason="restored", actor="analyst",
                        at=t1)
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, reason="cadence only",
                        actor="analyst", at=t1)
    # coverage_reason sits immediately after reason (code-review defect 1).
    assert ACCEPTED_CSV_HEADER.index("coverage_reason") == (
        ACCEPTED_CSV_HEADER.index("reason") + 1)
    data, _ = _accepted_exports(statements, inputs, state)
    assert next(csv.reader(io.StringIO(data.decode("utf-8-sig")))) == ACCEPTED_CSV_HEADER
    rows = _csv_rows(data)
    triples = {(r["framework_id"], r["statement_id"], r["control_id"]): r for r in rows}
    assert FALSE_POSITIVE not in triples
    assert len(rows) == _total_mappings(inputs) - 1
    assert {r["decision"] for r in rows} == {"accepted"}
    assert {r["policy_name"] for r in rows} == {POLICY_ID}
    d = triples[DOWNGRADE]
    assert (d["coverage"], d["ai_coverage"]) == ("partial", "full")
    assert (d["reason"], d["actor"], d["reviewed_at"]) == ("", "analyst", t1.isoformat())
    assert d["coverage_reason"] == "cadence only"
    other = triples[(NIST_ID, "P1", "AT-2")]                 # accepted with no metadata
    assert (other["reason"], other["actor"], other["reviewed_at"]) == ("", "", "")
    assert other["coverage_reason"] == ""


def test_accepted_xlsx_excludes_rejected_and_shows_it_as_gap(statements, inputs):
    _, wb = _accepted_exports(statements, inputs, _reviewed_state(inputs))
    assert set(wb.sheetnames) == {"Summary", "Crosswalk", "Mappings", "Gaps"}
    _, sid, cid = FALSE_POSITIVE

    mappings = _sheet_values(wb["Mappings"])
    h = {name: i for i, name in enumerate(mappings[0])}
    assert {"ai_coverage", "decision"} <= set(h)
    body = mappings[1:]
    assert not any(
        r[h["framework_id"]] == NIST_ID and r[h["statement_id"]] == sid
        and r[h["control_id"]] == cid for r in body
    )
    down = [r for r in body if (r[h["framework_id"]], r[h["statement_id"]],
                                r[h["control_id"]]) == DOWNGRADE]
    assert len(down) == 1
    assert down[0][h["coverage"]] == "partial" and down[0][h["ai_coverage"]] == "full"

    cross = _sheet_values(wb["Crosswalk"])
    col = cross[0].index(NIST_ID)
    p5 = next(r for r in cross[1:] if r[0] == sid)
    p2 = next(r for r in cross[1:] if r[0] == "P2")
    assert f"{cid} [" not in (p5[col] or "")
    assert "AC-2 [partial]" in p2[col]

    # SC-7 is only proposed via the rejected mapping, so it is now an uncovered gap.
    gaps = _sheet_values(wb["Gaps"])
    assert [NIST_ID, cid] in [r[:2] for r in gaps[1:]]


def test_accepted_xlsx_summary_says_reviewed_n_of_m(statements, inputs):
    state = _reviewed_state(inputs)
    _, wb = _accepted_exports(statements, inputs, state)
    total = _total_mappings(inputs)
    want = (f"Reviewed: accepted mappings only ({total - 1} of {total}). "
            "Pending and rejected mappings are excluded.")
    cells = [str(v) for row in _sheet_values(wb["Summary"]) for v in row if v is not None]
    assert want in cells
    assert f"Policy: {POLICY_ID}" in cells
    assert "Pending mappings: 0 (not counted as coverage)" in cells
    assert not any("AI-assisted draft" in c for c in cells)


def test_pending_cell_distinguishable_from_no_control_mapped(statements, inputs):
    """P1->AT-2 left pending (P1's only NIST mapping); P9 has no NIST mapping at all."""
    from control_mapper import review

    state = _start(review, inputs)
    _accept_all(state, review)
    review.set_status(state, _k(review, state, (NIST_ID, "P1", "AT-2")), review.ReviewStatus.PENDING)
    _, wb = _accepted_exports(statements, inputs, state)
    cross = _sheet_values(wb["Crosswalk"])
    col = cross[0].index(NIST_ID)
    p1 = next(r for r in cross[1:] if r[0] == "P1")[col]
    p9 = next(r for r in cross[1:] if r[0] == "P9")[col]
    assert p1 and p9
    assert p1 != p9, "pending-review cell looks identical to 'no control mapped'"
    assert p1.startswith("Pending review") and "AT-2" in p1
    assert p9 == "No control mapped"
    assert "AT-2 [full]" not in p1  # a pending mapping must not read as accepted


def test_all_pending_accepted_export_is_header_only_everything_a_gap(statements, inputs):
    from control_mapper import review
    from control_mapper.export import ACCEPTED_CSV_HEADER

    state = _start(review, inputs)
    data, wb = _accepted_exports(statements, inputs, state)
    assert list(csv.reader(io.StringIO(data.decode("utf-8-sig")))) == [ACCEPTED_CSV_HEADER]
    gaps = _sheet_values(wb["Gaps"])[1:]
    assert len(gaps) == sum(len(f.controls) for f in ALL_FRAMEWORKS)


def test_accepted_exports_touch_no_disk(statements, inputs, request):
    state = _reviewed_state(inputs)
    request.getfixturevalue("no_disk")
    data, wb = _accepted_exports(statements, inputs, state)
    assert data and wb.sheetnames


# ---------------------------------------------------------------------------
# Code-review defects (25 Nov): red until fixed
# ---------------------------------------------------------------------------


def test_coverage_reason_survives_later_status_changes(inputs):
    """Defect 1: set_status(PENDING/ACCEPTED) wiped the coverage-change reason."""
    from control_mapper import review

    S = review.ReviewStatus
    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, reason="x")
    review.set_status(state, dg, S.PENDING)
    review.set_status(state, dg, S.ACCEPTED)
    item = state.items[dg]
    assert item.coverage == CoverageLevel.PARTIAL
    assert item.coverage_reason == "x"
    # A reject with its own reason, then accept again: still the coverage reason.
    review.set_status(state, dg, S.REJECTED, reason="wrong control")
    review.set_status(state, dg, S.ACCEPTED)
    assert item.coverage_reason == "x"


def test_coverage_reason_written_only_by_set_coverage(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    fp = _k(review, state, FALSE_POSITIVE)
    assert state.items[fp].coverage_reason is None
    review.set_status(state, fp, review.ReviewStatus.REJECTED, reason="decision reason")
    assert state.items[fp].coverage_reason is None
    current = state.items[fp].coverage
    changed = CoverageLevel.FULL if current == CoverageLevel.PARTIAL else CoverageLevel.PARTIAL
    review.set_coverage(state, fp, changed, reason="coverage reason")  # a REAL change
    assert state.items[fp].coverage == changed
    assert state.items[fp].coverage_reason == "coverage reason"
    assert state.items[fp].reason == "decision reason"  # decision reason untouched
    review.set_coverage(state, fp, changed)  # unchanged: no-op
    assert state.items[fp].coverage_reason == "coverage reason"


def test_coverage_reason_carries_over_on_rerun(statements, inputs):
    from control_mapper import review

    old = _start(review, inputs, statements)
    dg = _k(review, old, DOWNGRADE)
    review.set_coverage(old, dg, CoverageLevel.PARTIAL, reason="cadence only")
    new = _start(review, inputs, statements, previous=old)
    assert new.items[dg].coverage_reason == "cadence only"


def test_accepted_exports_carry_coverage_reason_after_status_changes(statements, inputs):
    from control_mapper import review

    state = _start(review, inputs)
    _accept_all(state, review)
    dg = _k(review, state, DOWNGRADE)
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, reason="cadence only")
    review.set_status(state, dg, review.ReviewStatus.PENDING)
    review.set_status(state, dg, review.ReviewStatus.ACCEPTED)
    data, wb = _accepted_exports(statements, inputs, state)
    row = {(r["framework_id"], r["statement_id"], r["control_id"]): r
           for r in _csv_rows(data)}[DOWNGRADE]
    assert row["coverage_reason"] == "cadence only"
    assert row["reason"] == ""  # the accept carried no decision reason
    mappings = _sheet_values(wb["Mappings"])
    h = mappings[0]
    assert h.index("coverage_reason") == h.index("reason") + 1
    down = [r for r in mappings[1:] if (r[h.index("framework_id")], r[h.index("statement_id")],
                                        r[h.index("control_id")]) == DOWNGRADE]
    assert len(down) == 1 and down[0][h.index("coverage_reason")] == "cadence only"


RESTARTED_NUMBERING = "1. Reviewed annually.\n2. Logs are kept.\n1. MFA for remote access.\n"


def test_duplicate_statement_labels_raise_in_statement_keys():
    """Defect 2: P1, P2, P1 silently overwrote the first P1's key."""
    from control_mapper import review

    stmts = parse_policy(RESTARTED_NUMBERING)
    assert [s.id for s in stmts] == ["P1", "P2", "P1"]  # precondition: parser keeps them
    with pytest.raises(ValueError):
        review.statement_keys(POLICY_ID, stmts)


def test_duplicate_statement_labels_raise_in_start_review():
    from control_mapper import review

    stmts = parse_policy(RESTARTED_NUMBERING)
    inputs = [CrosswalkInput(NIST, ControlMapper(StubClient()).map(stmts, NIST))]
    with pytest.raises(ValueError):
        review.start_review(POLICY_ID, stmts, inputs)


def test_review_module_has_no_bulk_accept_path():
    """Defect 3 (pin): every decision is made one mapping at a time."""
    import inspect
    import re

    from control_mapper import review

    bulk = re.compile(r"(?i)bulk|batch|mass|(^|_)all($|_)|every|select")
    names = [n for n, obj in vars(review).items()
             if callable(obj) and not n.startswith("__") and bulk.search(n)]
    assert names == [], f"bulk-style callables in review.py: {names}"
    # set_status / set_coverage take ONE key, not a collection.
    for fn in (review.set_status, review.set_coverage):
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["state", "key"], (fn.__name__, params)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_accepted_export_with_raw_inputs_raises(statements, inputs, kind):
    """Defect 5: review=state with un-accepted inputs exported pending/rejected rows."""
    from control_mapper.export import crosswalk_csv_bytes, crosswalk_xlsx_bytes

    state = _reviewed_state(inputs)  # one rejected mapping
    fn = crosswalk_csv_bytes if kind == "csv" else crosswalk_xlsx_bytes
    with pytest.raises(ValueError):
        fn(build_crosswalk(statements, inputs), statements, inputs, review=state)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_accepted_export_all_pending_with_raw_inputs_raises(statements, inputs, kind):
    from control_mapper import review
    from control_mapper.export import crosswalk_csv_bytes, crosswalk_xlsx_bytes

    state = _start(review, inputs)
    fn = crosswalk_csv_bytes if kind == "csv" else crosswalk_xlsx_bytes
    with pytest.raises(ValueError):
        fn(build_crosswalk(statements, inputs), statements, inputs, review=state)


# ---- independent oracle for the draft exports (defect 4) ------------------------------
# Built from the inputs with the stdlib csv module and a plain openpyxl Workbook().save,
# never with export.py, so a bug shared by the bytes and path writers is caught.

ORACLE_CSV_HEADER = ["statement_id", "statement_text", "framework_id", "control_id",
                     "control_title", "coverage", "confidence", "rationale"]


def _oracle_records(statements, inputs):
    text = {s.id: s.text for s in statements}
    recs = []
    for inp in inputs:
        titles = {c.id: c.title for c in inp.framework.controls}
        for m in inp.result.mappings:
            recs.append([m.statement_id, text.get(m.statement_id, ""),
                         inp.framework.framework_id, m.control_id,
                         titles.get(m.control_id, ""), m.coverage.value,
                         round(m.confidence, 2), m.rationale])

    def number(label):  # spec: P2 before P10, then framework, then control
        digits = "".join(ch for ch in label if ch.isdigit())
        return int(digits) if digits else 0

    recs.sort(key=lambda r: (number(r[0]), r[0], r[2], r[3]))
    cells = [c for r in recs for c in r if isinstance(c, str)]
    assert not [c for c in cells if c.startswith(("=", "+", "-", "@", "\t", "\r"))], (
        "oracle precondition: sample data has no formula-trigger cells")
    return recs


def _oracle_csv_rows(statements, inputs):
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(ORACLE_CSV_HEADER)
    for r in _oracle_records(statements, inputs):
        w.writerow(r)
    return list(csv.reader(io.StringIO(buf.getvalue(), newline="")))


def _oracle_workbook(tmp_path, statements, inputs):
    from openpyxl import Workbook

    wb = Workbook()
    ms = wb.active
    ms.title = "Mappings"
    ms.append(ORACLE_CSV_HEADER)
    for r in _oracle_records(statements, inputs):
        ms.append(r)
    fids = [i.framework.framework_id for i in inputs]
    cs = wb.create_sheet("Crosswalk")
    cs.append(["Statement ID", "Statement"] + fids)
    for s in statements:
        row = [s.id, s.text]
        for inp in inputs:
            pairs = sorted((m.control_id, m.coverage.value) for m in inp.result.mappings
                           if m.statement_id == s.id)
            row.append("; ".join(f"{c} [{v}]" for c, v in pairs) if pairs
                       else "(gap / out of scope)")
        cs.append(row)
    gs = wb.create_sheet("Gaps")
    gs.append(["Framework", "Control ID", "Control Title", "Status"])
    for inp in inputs:
        touched = {m.control_id for m in inp.result.mappings}
        for c in inp.framework.controls:
            if c.id not in touched:
                gs.append([inp.framework.framework_id, c.id, c.title, "uncovered"])
    path = tmp_path / "independent-oracle.xlsx"
    wb.save(path)
    return load_workbook(path)


def test_draft_csv_matches_independent_oracle(tmp_path, statements, inputs):
    from control_mapper.export import crosswalk_csv_bytes

    want = _oracle_csv_rows(statements, inputs)
    cw = build_crosswalk(statements, inputs)
    got_bytes = list(csv.reader(io.StringIO(
        crosswalk_csv_bytes(cw, statements, inputs).decode("utf-8-sig"), newline="")))
    crosswalk_to_csv(cw, statements, inputs, tmp_path / "path.csv")
    with open(tmp_path / "path.csv", newline="", encoding="utf-8-sig") as fh:
        got_path = list(csv.reader(fh))
    assert len(want) == _total_mappings(inputs) + 1
    assert got_bytes == want
    assert got_path == want


@pytest.mark.parametrize("sheet", ["Mappings", "Crosswalk", "Gaps"])
def test_draft_xlsx_matches_independent_oracle(tmp_path, statements, inputs, sheet):
    from control_mapper.export import crosswalk_xlsx_bytes

    oracle = _oracle_workbook(tmp_path, statements, inputs)
    cw = build_crosswalk(statements, inputs)
    got_bytes = load_workbook(io.BytesIO(crosswalk_xlsx_bytes(cw, statements, inputs)))
    crosswalk_to_xlsx(cw, statements, inputs, tmp_path / "path.xlsx")
    got_path = load_workbook(tmp_path / "path.xlsx")
    want = _sheet_values(oracle[sheet])
    assert len(want) > 1
    assert _sheet_values(got_bytes[sheet]) == want
    assert _sheet_values(got_path[sheet]) == want


def test_draft_xlsx_summary_rows_match_independent_oracle(statements, inputs):
    from control_mapper.export import crosswalk_xlsx_bytes

    cw = build_crosswalk(statements, inputs)
    wb = load_workbook(io.BytesIO(crosswalk_xlsx_bytes(cw, statements, inputs)))
    rows = {r[0].value: r for r in wb["Summary"].iter_rows() if r[0].value}
    for inp in inputs:
        fid = inp.framework.framework_id
        assert fid in rows, f"Summary row for {fid} missing"
        r = rows[fid]
        assert r[1].value == len(inp.framework.controls)
        for c in r[2:5]:  # Mappings / Full / Partial stay live formulas over Mappings
            assert c.data_type == "f" and "Mappings!" in c.value, (fid, c.value)



def test_same_coverage_with_reason_is_a_no_op_after_reject(inputs):
    """ADR 0001 S1: re-saving the same coverage changes nothing, reason or not."""
    from control_mapper import review

    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)                      # coverage full
    review.set_status(state, dg, review.ReviewStatus.REJECTED, actor="a1",
                      reason="wrong control", at=SEED_AT)
    before = copy.deepcopy(state.items[dg])
    review.set_coverage(state, dg, CoverageLevel.FULL, actor="a2", reason="looks fine",
                        at=LATER_AT)
    item = state.items[dg]
    assert item == before
    assert (item.status, item.reason, item.coverage_reason) == (
        review.ReviewStatus.REJECTED, "wrong control", None)


def test_real_coverage_change_never_overwrites_decision_reason(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)
    review.set_status(state, dg, review.ReviewStatus.REJECTED, reason="wrong control")
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, actor="a2", reason="cadence",
                        at=LATER_AT)
    item = state.items[dg]
    assert (item.coverage, item.coverage_reason, item.actor, item.at) == (
        CoverageLevel.PARTIAL, "cadence", "a2", LATER_AT)
    assert (item.status, item.reason) == (review.ReviewStatus.REJECTED, "wrong control")


def test_coverage_reason_is_stored_trimmed(inputs):
    from control_mapper import review

    state = _start(review, inputs)
    dg = _k(review, state, DOWNGRADE)
    review.set_coverage(state, dg, CoverageLevel.PARTIAL, reason="  cadence only \n\t")
    assert state.items[dg].coverage_reason == "cadence only"
