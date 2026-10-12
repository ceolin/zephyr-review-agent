# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The review the model must hand back, and its validation against the diff."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ["critical", "high", "medium", "low", "suggestion"]
CATEGORIES = [
    "bug",
    "security",
    "concurrency",
    "build_config",
    "devicetree",
    "driver",
    "api",
    "error_handling",
    "portability",
    "tests",
    "documentation",
    "commit_message",
    "maintainability",
]
VERDICTS = ["approve", "comment", "request_changes"]
CONFIDENCES = ["high", "medium", "low"]

SUBMIT_TOOL_NAME = "submit_review"

SUBMIT_REVIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SUBMIT_TOOL_NAME,
        "description": (
            "Submit the final review. Call exactly once, after investigating with the other tools. "
            "Calling it ends the review."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "verdict",
                "confidence",
                "findings",
                "test_assessment",
                "commit_message_assessment",
            ],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Two to four sentences: what the PR changes, which "
                    "subsystems and configurations are affected, and the overall result.",
                },
                "verdict": {"type": "string", "enum": VERDICTS},
                "confidence": {
                    "type": "string",
                    "enum": CONFIDENCES,
                    "description": "How well the available context supported the review.",
                },
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "file",
                            "line",
                            "severity",
                            "category",
                            "title",
                            "problem",
                            "trigger",
                            "impact",
                            "recommendation",
                            "confidence",
                        ],
                        "properties": {
                            "file": {"type": "string", "description": "Repository-relative path as in the diff."},
                            "line": {
                                "type": "integer",
                                "description": "Line on the new side of the diff that the "
                                "finding concerns; 0 when not anchored to one changed line.",
                            },
                            "end_line": {"type": "integer", "description": "Last line of the range, or 0."},
                            "severity": {"type": "string", "enum": SEVERITIES},
                            "category": {"type": "string", "enum": CATEGORIES},
                            "title": {"type": "string", "description": "One short sentence naming the problem."},
                            "problem": {"type": "string", "description": "The concrete issue."},
                            "trigger": {
                                "type": "string",
                                "description": "Configuration, input, timing, architecture "
                                "or execution path required to expose it.",
                            },
                            "impact": {"type": "string", "description": "What breaks, or why it matters."},
                            "recommendation": {"type": "string", "description": "A practical fix or direction."},
                            "suggested_code": {
                                "type": "string",
                                "description": "Drop-in replacement for the cited "
                                "lines, correctly indented, no diff markers; empty when unhelpful.",
                            },
                            "confidence": {"type": "string", "enum": CONFIDENCES},
                            "evidence": {
                                "type": "string",
                                "description": "Which lookups support this finding, "
                                "e.g. 'read_file drivers/i2c/i2c_foo.c:120-160; find_references foo_init'.",
                            },
                        },
                    },
                },
                "test_assessment": {
                    "type": "string",
                    "description": "What behavior is tested, what material "
                    "behavior is not, and whether tests are required before merge. Empty when the "
                    "change needs no tests.",
                },
                "discussion_assessment": {
                    "type": "string",
                    "description": "How the existing review discussion stands against the current head: which "
                    "requests are addressed, which are still open, and any comment the code contradicts, with "
                    "evidence. Empty when there is no discussion.",
                },
                "commit_message_assessment": {
                    "type": "string",
                    "description": "Whether the commit messages "
                    "explain the why, match the change, and follow project rules. Empty "
                    "when fine.",
                },
            },
        },
    },
}


@dataclass
class Finding:
    file: str
    line: int
    severity: str
    category: str
    title: str
    problem: str
    trigger: str = ""
    impact: str = ""
    recommendation: str = ""
    suggested_code: str = ""
    confidence: str = "medium"
    evidence: str = ""
    end_line: int = 0
    inline: bool = False  # anchored to a line GitHub will accept an inline comment on
    notes: list[str] = field(default_factory=list)


@dataclass
class Review:
    summary: str
    verdict: str
    confidence: str
    findings: list[Finding]
    test_assessment: str = ""
    commit_message_assessment: str = ""
    discussion_assessment: str = ""

    def highest_severity(self) -> str:
        for sev in SEVERITIES:
            if any(f.severity == sev for f in self.findings):
                return sev
        return "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm_enum(value: Any, allowed: list[str], default: str) -> str:
    v = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if v in allowed:
        return v
    aliases = {
        "requestchanges": "request_changes",
        "changes_requested": "request_changes",
        "info": "suggestion",
        "nit": "suggestion",
        "minor": "low",
        "major": "high",
        "blocker": "critical",
    }
    return aliases.get(v, default)


def parse_review(args: dict[str, Any], changed_lines: dict[str, set[int]]) -> tuple[Review | None, list[str]]:
    """Validate ``submit_review`` arguments. Returns (review, errors); errors non-empty means reject and retry."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return None, ["arguments must be a JSON object"]
    summary = str(args.get("summary") or "").strip()
    if not summary:
        errors.append("summary is required")
    verdict = _norm_enum(args.get("verdict"), VERDICTS, "")
    if not verdict:
        errors.append(f"verdict must be one of {VERDICTS}")
    confidence = _norm_enum(args.get("confidence"), CONFIDENCES, "medium")
    raw_findings = args.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        errors.append("findings must be an array")
        raw_findings = []

    findings: list[Finding] = []
    for i, rf in enumerate(raw_findings):
        if not isinstance(rf, dict):
            errors.append(f"findings[{i}] must be an object")
            continue
        title = str(rf.get("title") or "").strip()
        problem = str(rf.get("problem") or rf.get("description") or "").strip()
        if not title or not problem:
            errors.append(f"findings[{i}] needs title and problem")
            continue
        path = str(rf.get("file") or "").strip().lstrip("/")
        if path.startswith(("a/", "b/")) and path[2:] in changed_lines:
            path = path[2:]
        try:
            line = int(rf.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        try:
            end_line = int(rf.get("end_line") or 0)
        except (TypeError, ValueError):
            end_line = 0
        f = Finding(
            file=path,
            line=max(line, 0),
            end_line=max(end_line, 0),
            severity=_norm_enum(rf.get("severity"), SEVERITIES, "medium"),
            category=_norm_enum(rf.get("category"), CATEGORIES, "bug"),
            title=title,
            problem=problem,
            trigger=str(rf.get("trigger") or "").strip(),
            impact=str(rf.get("impact") or "").strip(),
            recommendation=str(rf.get("recommendation") or "").strip(),
            suggested_code=str(rf.get("suggested_code") or "").rstrip(),
            confidence=_norm_enum(rf.get("confidence"), CONFIDENCES, "medium"),
            evidence=str(rf.get("evidence") or "").strip(),
        )
        if path and path not in changed_lines:
            f.notes.append("file is not changed by this pull request; shown in the summary only")
            f.line, f.end_line = 0, 0
        elif f.line:
            lines = changed_lines.get(path, set())
            if f.line in lines and (not f.end_line or f.end_line >= f.line):
                f.inline = True
                if f.end_line and any(n not in lines for n in range(f.line, f.end_line + 1)):
                    f.end_line = 0  # range leaves the diff; anchor on the single line instead
            else:
                f.notes.append(f"line {f.line} is not on the new side of the diff; shown in the summary only")
        findings.append(f)

    if errors:
        return None, errors
    review = Review(
        summary=summary,
        verdict=verdict,
        confidence=confidence,
        findings=findings,
        test_assessment=str(args.get("test_assessment") or "").strip(),
        commit_message_assessment=str(args.get("commit_message_assessment") or "").strip(),
        discussion_assessment=str(args.get("discussion_assessment") or "").strip(),
    )
    return review, []
