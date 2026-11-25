"""Red tests: owner-confirmed phase-1 security controls (offline, no server started).

CONTRACT
========
1. Network bind   .streamlit/config.toml exists and sets [server] address = "127.0.0.1".
                  Asserted on the parsed file only; no server is started.
2. Formula guard  In the DRAFT and ACCEPTED CSV and XLSX outputs (crosswalk_csv_bytes /
                  crosswalk_xlsx_bytes, review=None and review=state), no cell text may
                  start with = + - @ TAB or CR. Neutralise it, for example with a leading
                  single quote. In XLSX it is also acceptable to keep the text unchanged
                  if it is stored as a string (data_type "s", never "f"). The payload text
                  must still be in the output (neutralised, not dropped). The Summary
                  sheet's own COUNTIF/COUNTIFS formulas must stay formulas.
                  The draft bytes must stay identical to crosswalk_to_csv / _xlsx
                  (test_review.py), so the path writers get the same guard.
3. Source links   Every framework JSON has an https source_url whose host is on the
                  allowlist (csrc.nist.gov, www.iso.org, www.aicpa-cima.com); an optional
                  per-control source_url obeys the same rule. load_framework rejects any
                  other URL (ValueError / pydantic ValidationError). Links come only
                  from the framework JSON: the model tool schema and Mapping have no URL
                  field, and the accepted export's source_url column equals the JSON
                  value even if the model returns a URL.
4. No markdown    User or model text (statements, rationale) is never rendered through
                  st.markdown or anything markdown-based (st.write(str), caption, alerts,
                  headers). It may appear in dataframes. Checked on the Map and Crosswalk
                  tabs, which show the unmapped statements and gaps.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

# UI contract names live in ONE place (test_app_ui.py); reuse them and its offline fence.
from test_app_ui import (  # noqa: E402,F401
    ALL_L,
    MAP_BUTTON,
    NIST_L,
    _button,
    _by_label,
    _new_app,
    _run,
    _run_crosswalk,
    never_live,
)

from control_mapper.crosswalk import CrosswalkInput, build_crosswalk  # noqa: E402
from control_mapper.frameworks import load_framework, parse_policy  # noqa: E402
from control_mapper.mapper import ControlMapper  # noqa: E402

NIST = load_framework(ROOT / "data" / "frameworks" / "nist_800-53_subset.json")
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")

# ---------------------------------------------------------------------------
# 1. Network bind
# ---------------------------------------------------------------------------


def test_streamlit_config_binds_loopback_only():
    cfg = ROOT / ".streamlit" / "config.toml"
    assert cfg.is_file(), ".streamlit/config.toml missing"
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data.get("server", {}).get("address") == "127.0.0.1"


# ---------------------------------------------------------------------------
# 2. Formula-injection guard
# ---------------------------------------------------------------------------

STATEMENT_PAYLOADS = [
    '=HYPERLINK("http://x","y")',
    "+cmd|' /C calc'!A0",
    "-2+3+cmd|' /C calc'!A0",
    "@SUM(1+1)*cmd|' /C calc'!A0",
]
# Statement text cannot start with TAB/CR (parse_policy strips it); rationale can.
RATIONALE_PAYLOADS = [
    '=HYPERLINK("http://x","y")',
    "+cmd|' /C calc'!A0",
    "-1+1",
    "@SUM(1)",
    "\t=1+1",
    "\r=1+1",
]
CONTROLS = ["AT-2", "AC-6", "AC-2", "IA-2", "IA-5", "SC-28"]
POLICY_NAME_PAYLOAD = '=HYPERLINK("http://p","policy")'
REASON_PAYLOADS = ["@SUM(9)", "+cmd|' /C reason'!A0", "-9+9", "=1+2"]


class PayloadClient:
    """Offline model stand-in whose rationales carry spreadsheet-formula payloads."""

    def get_mappings(self, system, tool, user_content):
        return [
            {"statement_id": f"P{(i % len(STATEMENT_PAYLOADS)) + 1}", "control_id": cid,
             "coverage": "full", "confidence": 0.9, "rationale": RATIONALE_PAYLOADS[i]}
            for i, cid in enumerate(CONTROLS)
        ]


@pytest.fixture
def payload_run():
    policy = "\n".join(f"{i}. {p}" for i, p in enumerate(STATEMENT_PAYLOADS, start=1))
    statements = parse_policy(policy)
    assert [s.text for s in statements] == STATEMENT_PAYLOADS
    inputs = [CrosswalkInput(NIST, ControlMapper(PayloadClient()).map(statements, NIST))]
    assert len(inputs[0].result.mappings) == len(CONTROLS)
    return statements, inputs


def _draft(statements, inputs):
    from control_mapper.export import crosswalk_csv_bytes, crosswalk_xlsx_bytes

    cw = build_crosswalk(statements, inputs)
    return (crosswalk_csv_bytes(cw, statements, inputs),
            crosswalk_xlsx_bytes(cw, statements, inputs))


def _accepted(statements, inputs):
    from control_mapper import review
    from control_mapper.export import crosswalk_csv_bytes, crosswalk_xlsx_bytes

    state = review.start_review(POLICY_NAME_PAYLOAD, statements, inputs)
    for i, key in enumerate(list(state.items)):
        review.set_status(state, key, review.ReviewStatus.ACCEPTED,
                          reason=REASON_PAYLOADS[i % len(REASON_PAYLOADS)])
    acc = review.accepted_inputs(state, inputs)
    cw = build_crosswalk(statements, acc)
    return (crosswalk_csv_bytes(cw, statements, acc, review=state),
            crosswalk_xlsx_bytes(cw, statements, acc, review=state))


BUILDERS = {"draft": _draft, "accepted": _accepted}


def _assert_payloads_kept(texts):
    blob = "\n".join(texts)
    assert "HYPERLINK" in blob and "cmd|" in blob and "SUM(1" in blob, (
        "payload text was dropped instead of neutralised")


@pytest.mark.parametrize("kind", list(BUILDERS))
def test_csv_cells_never_start_with_a_formula_trigger(payload_run, kind):
    data, _ = BUILDERS[kind](*payload_run)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    cells = [c for row in rows[1:] for c in row]
    bad = [c for c in cells if c.startswith(DANGEROUS)]
    assert not bad, f"{kind} CSV has formula-trigger cells: {bad!r}"
    _assert_payloads_kept(cells)


def _xlsx_scan(wb):
    """-> (texts, offenders, intended Summary formulas)."""
    texts, offenders, summary_formulas = [], [], []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if not isinstance(v, str):
                    continue
                texts.append(v)
                intended = (ws.title == "Summary" and cell.data_type == "f"
                            and v.startswith(("=COUNTIF(", "=COUNTIFS(")))
                if intended:
                    summary_formulas.append(v)
                elif cell.data_type == "f":
                    offenders.append((ws.title, cell.coordinate, v))
    return texts, offenders, summary_formulas


def test_xlsx_scanner_detects_a_planted_formula():
    """Harness self-test (passes today): proves the XLSX gate can fail."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Mappings"
    wb.active.append(['=HYPERLINK("http://x","y")', "safe"])
    summ = wb.create_sheet("Summary")
    summ["C6"] = "=COUNTIF(Mappings!$C$2:$C$9,A6)"
    buf = io.BytesIO()
    wb.save(buf)
    _, offenders, formulas = _xlsx_scan(load_workbook(io.BytesIO(buf.getvalue())))
    assert [o[:2] for o in offenders] == [("Mappings", "A1")]
    assert formulas == ["=COUNTIF(Mappings!$C$2:$C$9,A6)"]


@pytest.mark.parametrize("kind", list(BUILDERS))
def test_xlsx_cells_are_never_live_formulas_except_summary(payload_run, kind):
    _, data = BUILDERS[kind](*payload_run)
    texts, offenders, summary_formulas = _xlsx_scan(load_workbook(io.BytesIO(data)))
    assert not offenders, f"{kind} XLSX live/unsafe cells: {offenders!r}"
    # The Summary's own counts must still be formulas (Mappings/Full/Partial per fw).
    assert len(summary_formulas) >= 3, summary_formulas
    _assert_payloads_kept(texts)


# ---------------------------------------------------------------------------
# 3. No markdown rendering of user/model text
# ---------------------------------------------------------------------------

MD_PAYLOAD = "See ![x](http://example.invalid/a.png) and **bold** here."
MD_MARKERS = ("example.invalid", "**bold**")
POLICY_LABEL = "Policy text (numbered statements)"  # today's label = notes.md copy deck


def _policy_area(app):
    return _by_label(app.text_area, POLICY_LABEL, "text_area")


def _policy_with_md_statement():
    sample = (ROOT / "data" / "sample_policies" / "sample_policy.md").read_text()
    return sample.rstrip() + f"\n11. {MD_PAYLOAD}\n"  # P11: unmapped in every framework


def _markdown_texts(app):
    out = []
    for kind in ("markdown", "caption", "warning", "info", "error", "success",
                 "title", "header", "subheader", "latex", "code"):
        out += [(kind, str(e.value)) for e in getattr(app, kind)]
    return out


def _dataframe_texts(app):
    out = []
    for df in app.dataframe:
        out.append(df.value.astype(str).to_csv())
    return out


def _assert_no_markdown_payload(app, where):
    leaks = [(k, t) for k, t in _markdown_texts(app) if any(m in t for m in MD_MARKERS)]
    assert not leaks, f"user text rendered as markdown on {where}: {leaks!r}"
    assert any("example.invalid" in t for t in _dataframe_texts(app)), (
        f"{where}: unmapped statement not shown in any dataframe")


def test_map_tab_never_renders_statement_text_as_markdown():
    app = _new_app()
    _policy_area(app).set_value(_policy_with_md_statement())
    _run(app)
    _button(app, MAP_BUTTON).click()
    _run(app)
    _assert_no_markdown_payload(app, "Map tab")


def test_crosswalk_tab_never_renders_statement_text_as_markdown():
    app = _new_app()
    _policy_area(app).set_value(_policy_with_md_statement())
    _run(app)
    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    _assert_no_markdown_payload(app, "Crosswalk/Review tabs")


# ---------------------------------------------------------------------------
# 4. Source links: https + allowlisted host, only from framework JSON
# ---------------------------------------------------------------------------

import json  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

ALLOWED_HOSTS = {"csrc.nist.gov", "www.iso.org", "www.aicpa-cima.com"}
FW_FILES = sorted((ROOT / "data" / "frameworks").glob("*.json"))


def _url_ok(url):
    u = urlparse(url)
    return u.scheme == "https" and u.hostname in ALLOWED_HOSTS and not u.username \
        and not u.password and u.port is None


@pytest.mark.parametrize("path", FW_FILES, ids=[p.stem for p in FW_FILES])
def test_framework_source_urls_are_https_and_allowlisted(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "source_url" in data, f"{path.name}: framework-level source_url missing"
    assert _url_ok(data["source_url"]), data["source_url"]
    for c in data["controls"]:
        if "source_url" in c:
            assert _url_ok(c["source_url"]), (c["id"], c["source_url"])


@pytest.mark.parametrize("bad", [
    "http://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final",
    "https://evil.example/pubs",
    "https://csrc.nist.gov.evil.example/x",
    "javascript:alert(1)",
    "https://user:pw@csrc.nist.gov/x",
], ids=["http", "other-host", "suffix-trick", "javascript", "userinfo"])
def test_load_framework_rejects_unsafe_source_url(tmp_path, bad):
    data = json.loads((ROOT / "data" / "frameworks" / "nist_800-53_subset.json").read_text())
    data["source_url"] = bad
    f = tmp_path / "fw.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_framework(f)


GOOD_URL = "https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final"
# Code-review defect 6: characters that break out of a Markdown link or an HTML
# attribute, plus whitespace and every control character (0x00-0x1f and 0x7f).
LINK_BREAKERS = list("()[]<>\"'`") + [" ", "\t", "\n", "\r", "\x0b", "\x0c", "\u00a0",
                                      "\u2028", "\x00", "\x1b", "\x1f", "\x7f"]


def _char_id(ch):
    return f"U+{ord(ch):04X}"


def test_check_source_url_accepts_a_clean_allowlisted_url():
    from control_mapper.models import check_source_url

    assert check_source_url(GOOD_URL) == GOOD_URL


@pytest.mark.parametrize("where", ["path", "query", "fragment"])
@pytest.mark.parametrize("ch", LINK_BREAKERS, ids=[_char_id(c) for c in LINK_BREAKERS])
def test_check_source_url_rejects_link_breaking_characters(ch, where):
    from control_mapper.models import check_source_url

    url = {"path": f"{GOOD_URL}{ch}x",
           "query": f"{GOOD_URL}?q=a{ch}b",
           "fragment": f"{GOOD_URL}#a{ch}b"}[where]
    with pytest.raises(ValueError):
        check_source_url(url)


@pytest.mark.parametrize("ch", ["(", ")", "]", "\x7f"], ids=_char_id)
def test_load_framework_rejects_link_breaking_source_url(tmp_path, ch):
    data = json.loads((ROOT / "data" / "frameworks" / "nist_800-53_subset.json").read_text())
    data["controls"][0]["source_url"] = f"{GOOD_URL}{ch}x"
    f = tmp_path / "fw.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_framework(f)


def test_model_output_cannot_carry_a_link():
    from control_mapper.mapper import SUBMIT_MAPPINGS_TOOL
    from control_mapper.models import Mapping

    props = SUBMIT_MAPPINGS_TOOL["input_schema"]["properties"]["mappings"]["items"]
    assert not any("url" in k.lower() for k in props["properties"])
    assert props.get("additionalProperties") is False
    assert not any("url" in k.lower() for k in Mapping.model_fields)


class UrlClient(PayloadClient):
    def get_mappings(self, system, tool, user_content):
        rows = super().get_mappings(system, tool, user_content)
        for r in rows:
            r["source_url"] = "https://evil.example/phish"
            r["rationale"] = "see https://evil.example/phish"
        return rows


def test_export_source_url_comes_from_framework_json_only():
    from control_mapper import review
    from control_mapper.export import crosswalk_csv_bytes

    expected = json.loads(
        (ROOT / "data" / "frameworks" / "nist_800-53_subset.json").read_text()
    ).get("source_url")
    assert expected, "framework JSON source_url missing"
    policy = "\n".join(f"{i}. {p}" for i, p in enumerate(STATEMENT_PAYLOADS, start=1))
    statements = parse_policy(policy)
    inputs = [CrosswalkInput(NIST, ControlMapper(UrlClient()).map(statements, NIST))]
    state = review.start_review("url-policy", statements, inputs)
    for key in list(state.items):
        review.set_status(state, key, review.ReviewStatus.ACCEPTED)
    acc = review.accepted_inputs(state, inputs)
    data = crosswalk_csv_bytes(build_crosswalk(statements, acc), statements, acc,
                               review=state)
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows and {r["source_url"] for r in rows} == {expected}


def test_saved_reason_is_never_rendered_as_markdown():
    from test_app_ui import FP_KEY, _decide

    app = _new_app()
    _run_crosswalk(app, ALL_L, anchor=NIST_L)
    _decide(app, FP_KEY, "Rejected", reason=MD_PAYLOAD)
    _run(app)
    leaks = [(k, t) for k, t in _markdown_texts(app) if any(m in t for m in MD_MARKERS)]
    assert not leaks, f"reason rendered as markdown: {leaks!r}"
    assert any("example.invalid" in t for t in _dataframe_texts(app)), (
        "saved reason not shown in the review table")
