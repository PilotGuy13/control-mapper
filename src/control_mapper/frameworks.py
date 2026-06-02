"""Loading and parsing helpers.

Turns JSON framework files into `Framework` objects and turns a free-text policy
document into a list of discrete `PolicyStatement`s. The policy parser is
intentionally simple and explicit -- numbered lines become statements -- so the
splitting behaviour is transparent and testable rather than hidden inside a model
call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from .models import Framework, PolicyStatement


def load_framework(path: str | Path) -> Framework:
    """Load a reference framework from a JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Framework.model_validate(data)


# Matches lines that begin with a number followed by '.' or ')', e.g. "1." or "10)".
_NUMBERED_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")


def parse_policy(text: str) -> List[PolicyStatement]:
    """Split a policy document into statements.

    A statement starts at a numbered line and continues across wrapped/indented
    lines until the next numbered line. Lines before the first number (titles,
    preamble) are ignored.
    """
    statements: List[PolicyStatement] = []
    current_id: str | None = None
    current_parts: List[str] = []

    def flush() -> None:
        if current_id is not None and current_parts:
            body = " ".join(part.strip() for part in current_parts).strip()
            if body:
                statements.append(PolicyStatement(id=f"P{current_id}", text=body))

    for line in text.splitlines():
        match = _NUMBERED_LINE.match(line)
        if match:
            flush()
            current_id, first = match.group(1), match.group(2)
            current_parts = [first]
        elif current_id is not None and line.strip():
            current_parts.append(line)

    flush()
    return statements


def load_policy_file(path: str | Path) -> List[PolicyStatement]:
    return parse_policy(Path(path).read_text(encoding="utf-8"))
