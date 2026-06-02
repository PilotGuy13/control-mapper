"""Core mapping engine.

Given a list of policy statements and a reference framework, this asks Claude to
identify which controls each statement addresses, at what coverage level, with a
confidence score and a short rationale.

Two design choices worth calling out for reviewers:

1. **Forced structured output.** We define a tool with a strict JSON schema and
   set `tool_choice` to require it. The model cannot reply with prose; it must
   return data shaped exactly like our `Mapping` model. That makes the output
   parseable and testable instead of something we scrape with regexes.

2. **A pluggable client.** `MapperClient` is a Protocol. The real implementation
   wraps the Anthropic SDK; the test/eval suite injects a deterministic stub.
   This means the analysis, CLI, and tests all run offline with no API key and
   no cost, while production uses the live model.
"""

from __future__ import annotations

import json
import os
from typing import List, Protocol

from .models import Framework, Mapping, MappingResult, PolicyStatement

DEFAULT_MODEL = "claude-sonnet-4-20250514"

# The judgment lives here. This system prompt is the real intellectual property of
# the tool: it tells the model how a control assessor should reason -- conservative
# on confidence, explicit that "no matching control" is a valid and useful answer,
# and that a rationale must cite the specific obligation, not hand-wave.
SYSTEM_PROMPT = """\
You are a senior GRC control assessor. You map organizational policy statements to \
controls from a named compliance framework.

Rules:
- A statement may map to zero, one, or several controls. Mapping to zero controls \
is a legitimate and important outcome; do not invent a weak link just to produce \
a mapping.
- Use coverage = "full" only when the statement clearly satisfies the intent of the \
control on its own. Use "partial" when it addresses part of the control or one of \
several requirements. Never emit coverage = "none"; simply omit non-mappings.
- confidence reflects how certain the mapping is, not how strong the control is. \
Be conservative: reserve confidence above 0.85 for unambiguous matches.
- rationale must reference the specific obligation in the statement and why it \
addresses that control. One or two sentences. No filler.
- Only use control ids that appear in the provided framework. Never use ids from \
memory or from other frameworks.
"""

# JSON schema handed to the model as a tool. The model is forced to call this tool,
# so its entire reply is an object matching this shape.
SUBMIT_MAPPINGS_TOOL = {
    "name": "submit_mappings",
    "description": "Submit the policy-to-control mappings you have identified.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "statement_id": {"type": "string"},
                        "control_id": {"type": "string"},
                        "coverage": {"type": "string", "enum": ["full", "partial"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "statement_id",
                        "control_id",
                        "coverage",
                        "confidence",
                        "rationale",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["mappings"],
        "additionalProperties": False,
    },
}


class MapperClient(Protocol):
    """Minimal interface the mapper needs. Anything implementing this works."""

    def get_mappings(
        self, system: str, tool: dict, user_content: str
    ) -> List[dict]:
        """Return a list of raw mapping dicts matching the tool schema."""
        ...


class AnthropicClient:
    """Production client that wraps the Anthropic SDK.

    Imported lazily so the rest of the project (and the offline test suite) does
    not require the SDK or an API key to be installed/present.
    """

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 4096) -> None:
        # API key is read by the SDK from the ANTHROPIC_API_KEY environment
        # variable. We never accept it as an argument and never log it.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it or use an offline client."
            )
        from anthropic import Anthropic  # lazy import

        self._client = Anthropic()
        self._model = model
        self._max_tokens = max_tokens

    def get_mappings(self, system: str, tool: dict, user_content: str) -> List[dict]:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user_content}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return block.input.get("mappings", [])
        return []


def _build_user_content(
    statements: List[PolicyStatement], framework: Framework
) -> str:
    """Render the framework and statements into a single, unambiguous prompt."""
    controls_text = "\n".join(
        f"- {c.id} | {c.family} | {c.title}: {c.description}"
        for c in framework.controls
    )
    statements_text = "\n".join(f"- {s.id}: {s.text}" for s in statements)
    return (
        f"FRAMEWORK: {framework.framework_name} ({framework.framework_id})\n\n"
        f"CONTROLS:\n{controls_text}\n\n"
        f"POLICY STATEMENTS:\n{statements_text}\n\n"
        "Identify every defensible mapping, then call submit_mappings."
    )


class ControlMapper:
    """Maps policy statements to framework controls using a `MapperClient`."""

    def __init__(self, client: MapperClient) -> None:
        self._client = client

    def map(
        self, statements: List[PolicyStatement], framework: Framework
    ) -> MappingResult:
        user_content = _build_user_content(statements, framework)
        raw = self._client.get_mappings(
            SYSTEM_PROMPT, SUBMIT_MAPPINGS_TOOL, user_content
        )

        valid_ids = framework.control_ids()
        statement_ids = {s.id for s in statements}
        mappings: List[Mapping] = []
        for item in raw:
            try:
                mapping = Mapping.model_validate(item)
            except Exception:
                # Skip malformed rows rather than crashing the whole run; a single
                # bad row should never lose the good ones.
                continue
            # Guardrail: discard any control id the model invented or any statement
            # id it hallucinated. The model is not trusted to stay in bounds.
            if mapping.control_id in valid_ids and mapping.statement_id in statement_ids:
                mappings.append(mapping)

        return MappingResult(framework_id=framework.framework_id, mappings=mappings)


def result_to_json(result: MappingResult) -> str:
    return json.dumps(result.model_dump(), indent=2)
