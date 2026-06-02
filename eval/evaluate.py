"""Evaluation harness (multi-framework).

Scores a `MappingResult` against the hand-labeled gold standard for the chosen
framework and prints precision, recall, F1, plus the exact false positives and
false negatives so failures are inspectable rather than hidden behind one number.

This is the part that speaks to the real fear a hiring manager has about AI: that
it is confidently wrong. Measuring accuracy, and showing *where* the model fails,
is the difference between "I called an API" and "I can tell you whether to trust
the output."

Gold sets live in eval/gold/<framework_id>.json and are selected automatically from
the framework file's framework_id.

Run offline (uses the stub client, no key needed):
    PYTHONPATH=src:. python -m eval.evaluate --offline
    PYTHONPATH=src:. python -m eval.evaluate --offline --framework data/frameworks/iso_27001_2022_annexa_subset.json
    PYTHONPATH=src:. python -m eval.evaluate --offline --all

Run against the live model:
    PYTHONPATH=src:. python -m eval.evaluate --all
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from control_mapper.frameworks import load_framework, load_policy_file
from control_mapper.mapper import AnthropicClient, ControlMapper
from control_mapper.models import MappingResult

ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = ROOT / "eval" / "gold"
FRAMEWORK_DIR = ROOT / "data" / "frameworks"

# Default set of frameworks evaluated by --all, in display order.
ALL_FRAMEWORKS = [
    FRAMEWORK_DIR / "nist_800-53_subset.json",
    FRAMEWORK_DIR / "iso_27001_2022_annexa_subset.json",
    FRAMEWORK_DIR / "soc2_tsc_subset.json",
]

Pair = tuple[str, str]  # (statement_id, control_id)


@dataclass
class Scores:
    framework_id: str
    true_positives: list[Pair]
    false_positives: list[Pair]
    false_negatives: list[Pair]
    coverage_matches: int  # of the TPs, how many had the right coverage level

    @property
    def precision(self) -> float:
        tp, fp = len(self.true_positives), len(self.false_positives)
        return tp / (tp + fp) if (tp + fp) else 0.0

    @property
    def recall(self) -> float:
        tp, fn = len(self.true_positives), len(self.false_negatives)
        return tp / (tp + fn) if (tp + fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def coverage_accuracy(self) -> float:
        n = len(self.true_positives)
        return self.coverage_matches / n if n else 0.0


def load_gold(framework_id: str) -> tuple[set[Pair], dict[Pair, str], str]:
    path = GOLD_DIR / f"{framework_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No gold standard for framework '{framework_id}' at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs: set[Pair] = set()
    coverage: dict[Pair, str] = {}
    for m in data["mappings"]:
        key = (m["statement_id"], m["control_id"])
        pairs.add(key)
        coverage[key] = m["coverage"]
    return pairs, coverage, data["policy_file"]


def score(
    result: MappingResult, gold_pairs: set[Pair], gold_cov: dict[Pair, str]
) -> Scores:
    predicted: dict[Pair, str] = {
        (m.statement_id, m.control_id): m.coverage.value for m in result.mappings
    }
    predicted_pairs = set(predicted)
    tps = sorted(predicted_pairs & gold_pairs)
    fps = sorted(predicted_pairs - gold_pairs)
    fns = sorted(gold_pairs - predicted_pairs)
    coverage_matches = sum(1 for p in tps if predicted[p] == gold_cov[p])
    return Scores(result.framework_id, tps, fps, fns, coverage_matches)


def _fmt_pairs(pairs: Iterable[Pair]) -> str:
    pairs = list(pairs)
    if not pairs:
        return "    (none)"
    return "\n".join(f"    {sid} -> {cid}" for sid, cid in pairs)


def _make_client(offline: bool):
    if offline:
        from eval.stub_client import StubClient

        return StubClient()
    return AnthropicClient()


def run_one(framework_path: Path, offline: bool) -> Scores:
    framework = load_framework(framework_path)
    gold_pairs, gold_cov, policy_file = load_gold(framework.framework_id)
    statements = load_policy_file(ROOT / policy_file)
    result = ControlMapper(_make_client(offline)).map(statements, framework)
    return score(result, gold_pairs, gold_cov)


def _print_scores(s: Scores) -> None:
    print(f"EVALUATION  |  {s.framework_id}")
    print("=" * 56)
    print(f"  Precision        {s.precision:6.1%}")
    print(f"  Recall           {s.recall:6.1%}")
    print(f"  F1               {s.f1:6.1%}")
    print(f"  Coverage acc.    {s.coverage_accuracy:6.1%}  (of correct mappings)")
    print()
    print(f"  True positives   ({len(s.true_positives)})")
    print(f"  False positives  ({len(s.false_positives)})  -- model proposed, gold did not:")
    print(_fmt_pairs(s.false_positives))
    print(f"  False negatives  ({len(s.false_negatives)})  -- gold has, model missed:")
    print(_fmt_pairs(s.false_negatives))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate control mappings vs gold.")
    parser.add_argument("--offline", action="store_true", help="Use the stub client.")
    parser.add_argument(
        "--framework",
        default=str(FRAMEWORK_DIR / "nist_800-53_subset.json"),
        help="Path to a framework JSON file.",
    )
    parser.add_argument(
        "--all", action="store_true", help="Evaluate every bundled framework."
    )
    args = parser.parse_args(argv)

    targets = ALL_FRAMEWORKS if args.all else [Path(args.framework)]
    all_scores = [run_one(p, args.offline) for p in targets]
    for s in all_scores:
        _print_scores(s)

    if len(all_scores) > 1:
        print("SUMMARY")
        print("-" * 56)
        for s in all_scores:
            print(
                f"  {s.framework_id:24s}  P {s.precision:5.1%}  "
                f"R {s.recall:5.1%}  F1 {s.f1:5.1%}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
