"""Command-line interface.

Single-framework mapping:
    python -m control_mapper.cli \
        --policy data/sample_policies/sample_policy.md \
        --framework data/frameworks/nist_800-53_subset.json --offline

Cross-framework crosswalk (maps the same policy against several frameworks and
shows, per obligation, the equivalent controls in each):
    python -m control_mapper.cli \
        --policy data/sample_policies/sample_policy.md \
        --crosswalk \
        --framework data/frameworks/nist_800-53_subset.json \
        --framework data/frameworks/iso_27001_2022_annexa_subset.json \
        --framework data/frameworks/soc2_tsc_subset.json \
        --offline

Offline mode uses the bundled deterministic stub (no API key, no network, no cost).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis import analyze, render_report
from .crosswalk import (
    CrosswalkInput,
    build_crosswalk,
    equivalences_for,
    render_crosswalk,
    render_equivalences,
)
from .frameworks import load_framework, load_policy_file
from .mapper import AnthropicClient, ControlMapper, result_to_json


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="control-mapper",
        description="Map policy statements to compliance controls and crosswalk frameworks.",
    )
    p.add_argument("--policy", required=True, help="Path to a policy document.")
    p.add_argument(
        "--framework",
        action="append",
        required=True,
        dest="frameworks",
        help="Path to a framework JSON file. Repeat for multiple (use with --crosswalk).",
    )
    p.add_argument(
        "--crosswalk",
        action="store_true",
        help="Map against every provided framework and print a cross-framework crosswalk.",
    )
    p.add_argument(
        "--equivalences",
        metavar="FRAMEWORK_ID",
        help="With --crosswalk, also print control equivalences anchored on this framework_id.",
    )
    p.add_argument(
        "--export-csv",
        metavar="PATH",
        help="With --crosswalk, write the long-format crosswalk to this CSV path.",
    )
    p.add_argument(
        "--export-xlsx",
        metavar="PATH",
        help="With --crosswalk, write a formatted multi-sheet workbook to this XLSX path.",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic stub client (no API key/network required).",
    )
    p.add_argument("--json", dest="json_out", help="Write JSON output to this path.")
    p.add_argument("--model", default=None, help="Override the Claude model id.")
    return p


def _get_client(offline: bool, model: str | None):
    if offline:
        from eval.stub_client import StubClient  # type: ignore

        return StubClient()
    return AnthropicClient(model=model) if model else AnthropicClient()


def _run_single(args, statements) -> int:
    framework = load_framework(args.frameworks[0])
    try:
        client = _get_client(args.offline, args.model)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mapper = ControlMapper(client)
    result = mapper.map(statements, framework)
    analysis = analyze(result, framework, statements)

    if args.json_out:
        Path(args.json_out).write_text(result_to_json(result), encoding="utf-8")
        print(f"Wrote {len(result.mappings)} mappings to {args.json_out}")

    print(render_report(result, analysis, framework, statements))
    return 0


def _run_crosswalk(args, statements) -> int:
    try:
        client = _get_client(args.offline, args.model)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mapper = ControlMapper(client)
    inputs = []
    for fpath in args.frameworks:
        framework = load_framework(fpath)
        result = mapper.map(statements, framework)
        inputs.append(CrosswalkInput(framework=framework, result=result))

    crosswalk = build_crosswalk(statements, inputs)

    if args.export_csv:
        from .export import crosswalk_to_csv

        n = crosswalk_to_csv(crosswalk, statements, inputs, args.export_csv)
        print(f"Wrote {n} mapping rows to {args.export_csv}")
    if args.export_xlsx:
        from .export import crosswalk_to_xlsx

        crosswalk_to_xlsx(crosswalk, statements, inputs, args.export_xlsx)
        print(f"Wrote workbook to {args.export_xlsx}")

    if args.json_out:
        import json

        Path(args.json_out).write_text(
            json.dumps(crosswalk.to_dict(), indent=2), encoding="utf-8"
        )
        print(f"Wrote crosswalk to {args.json_out}\n")

    print(render_crosswalk(crosswalk, statements))

    if args.equivalences:
        print()
        equivs = equivalences_for(crosswalk, args.equivalences)
        print(render_equivalences(equivs, args.equivalences))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    statements = load_policy_file(args.policy)
    if not statements:
        print("No policy statements found. Are lines numbered '1.', '2.' ...?")
        return 1

    if args.crosswalk:
        return _run_crosswalk(args, statements)
    return _run_single(args, statements)


if __name__ == "__main__":
    raise SystemExit(main())
