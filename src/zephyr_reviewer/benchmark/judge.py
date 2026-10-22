# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""An LLM judge: did any finding describe the known bug?"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from ..review.openrouter import OpenRouter
from ..review.schema import Review

VERDICTS = ["DETECTED", "PARTIALLY_DETECTED", "MISSED"]

JUDGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "judge_detection",
        "description": "Record whether the reviewer's findings describe the known bug.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdict", "matched_finding", "explanation"],
            "properties": {
                "verdict": {"type": "string", "enum": VERDICTS},
                "matched_finding": {
                    "type": "integer",
                    "description": "1-based index of the finding that describes the bug, or 0 when none does.",
                },
                "explanation": {
                    "type": "string",
                    "description": "One sentence naming the matching finding, or why none matches.",
                },
            },
        },
    },
}

SYSTEM = """\
You grade an automated code reviewer against a known bug. You are given the ground truth, taken from
the commit that later fixed the bug, and the reviewer's findings on the commit that introduced it.

Decide strictly:
- DETECTED: one finding describes this specific problem: same defect, same mechanism, right place.
  Different wording is fine; a different bug in the same function is not.
- PARTIALLY_DETECTED: a finding points at the right code and the right kind of problem but gets the
  mechanism, trigger or consequence partly wrong, or is far more vague than the ground truth.
- MISSED: no finding describes it. Generic warnings, style notes and unrelated bugs do not count.

Do not reward volume: a review with many findings that circle the area without naming the defect is
MISSED. Use the judge_detection tool exactly once.\
"""


@dataclass
class Judgement:
    verdict: str
    matched_finding: int
    explanation: str
    model: str = ""
    usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def findings_digest(review: Review | None) -> str:
    if review is None:
        return "(the review did not complete; no findings)"
    if not review.findings:
        return "(no findings)"
    out = []
    for i, f in enumerate(review.findings, 1):
        loc = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "general")
        out.append(f"{i}. [{f.severity}] {f.title} ({loc})\n   Problem: {f.problem}")
        if f.trigger:
            out.append(f"   Trigger: {f.trigger}")
        if f.impact:
            out.append(f"   Impact: {f.impact}")
    return "\n".join(out)


def judge(llm: OpenRouter, problem_description: str, review: Review | None, commit_subject: str = "") -> Judgement:
    user = "\n".join(
        [
            f"## Commit under review\n{commit_subject or '(unknown)'}",
            "",
            "## Ground truth (from the fixing commit)",
            problem_description,
            "",
            "## Reviewer findings",
            findings_digest(review),
        ]
    )
    result = llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        tools=[JUDGE_TOOL],
        tool_choice={"type": "function", "function": {"name": "judge_detection"}},
    )
    args: dict[str, Any] = {}
    for tc in result.tool_calls:
        fn = tc.get("function") or {}
        if fn.get("name") == "judge_detection":
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {}
            break
    verdict = str(args.get("verdict") or "").upper()
    if verdict not in VERDICTS:
        verdict = "MISSED"
    try:
        matched = int(args.get("matched_finding") or 0)
    except (TypeError, ValueError):
        matched = 0
    if review is None or matched < 0 or matched > len(review.findings):
        matched = 0
    if verdict == "MISSED":
        matched = 0
    return Judgement(verdict, matched, str(args.get("explanation") or ""), result.model, result.usage)
