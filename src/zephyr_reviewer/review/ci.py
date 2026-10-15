# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""CI results for the head commit: check runs, legacy statuses, and the tail of failed job logs.

Two local tools. ``ci_status`` lists every check with its conclusion; ``ci_failure_logs`` returns a
bounded tail of a failed GitHub Actions job's log so the model can diagnose a compliance or twister
failure instead of ignoring it or repeating it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .github_api import GitHub, GitHubError

CI_STATUS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ci_status",
        "description": "CI results for the pull request's head commit: every check run and commit status with its "
        "conclusion (success, failure, neutral, skipped, pending), who produced it, and a link. Call before "
        "judging test coverage or build correctness; use ci_failure_logs for a failed job's output.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
    },
}

CI_FAILURE_LOGS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ci_failure_logs",
        "description": "Tail of the log of a failed GitHub Actions job on the head commit. Identify the job by a "
        "substring of its check name (as listed by ci_status) or its numeric id. Returns the last tail_lines "
        "lines with timestamps removed; pass a grep pattern to see only matching lines (plus a little context).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["job"],
            "properties": {
                "job": {"type": "string", "description": "Check name substring or job id."},
                "tail_lines": {"type": "integer", "description": "Lines from the end of the log (default 200)."},
                "grep": {"type": "string", "description": "Case-insensitive regex; only matching lines are shown."},
            },
        },
    },
}

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class Check:
    id: int
    name: str
    status: str
    conclusion: str
    app: str
    url: str
    is_actions: bool
    summary: str = ""


@dataclass
class CIResults:
    gh: GitHub
    sha: str
    checks: list[Check] = field(default_factory=list)
    loaded: bool = False
    max_chars: int = 24_000
    error: str = ""

    def load(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            for r in self.gh.check_runs(self.sha):
                app = (r.get("app") or {}).get("slug") or ""
                self.checks.append(
                    Check(
                        id=int(r.get("id") or 0),
                        name=r.get("name") or "",
                        status=r.get("status") or "",
                        conclusion=r.get("conclusion") or "",
                        app=app,
                        url=r.get("html_url") or r.get("details_url") or "",
                        is_actions=app == "github-actions",
                        summary=((r.get("output") or {}).get("title") or "")[:200],
                    )
                )
            for st in self.gh.commit_statuses(self.sha):
                self.checks.append(
                    Check(
                        id=0,
                        name=st.get("context") or "",
                        status="completed",
                        conclusion=st.get("state") or "",
                        app="status",
                        url=st.get("target_url") or "",
                        is_actions=False,
                        summary=(st.get("description") or "")[:200],
                    )
                )
        except GitHubError as exc:
            self.error = f"CI results unavailable: {exc}"

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.conclusion in ("failure", "timed_out", "action_required", "error")]

    def summary_line(self) -> str:
        self.load()
        if self.error:
            return self.error
        if not self.checks:
            return "No CI results on the head commit."
        counts: dict[str, int] = {}
        for c in self.checks:
            key = c.conclusion or c.status or "unknown"
            counts[key] = counts.get(key, 0) + 1
        parts = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        failed = ", ".join(c.name for c in self.failed[:8])
        return f"CI on head: {parts}." + (f" Failed: {failed}." if failed else "")

    async def status_tool(self, args: dict[str, Any]) -> str:
        self.load()
        if self.error:
            return self.error
        if not self.checks:
            return "No check runs or statuses on the head commit yet."
        order = {
            "failure": 0,
            "timed_out": 0,
            "action_required": 0,
            "error": 0,
            "pending": 1,
            "": 1,
            "neutral": 2,
            "skipped": 3,
            "cancelled": 3,
            "success": 4,
        }
        lines = [f"{len(self.checks)} check(s) on {self.sha[:12]}:"]
        for c in sorted(self.checks, key=lambda c: (order.get(c.conclusion, 2), c.name)):
            state = c.conclusion or c.status
            ident = f" [job {c.id}]" if c.is_actions else ""
            extra = f" - {c.summary}" if c.summary else ""
            lines.append(f"- {state:10s} {c.name}{ident} ({c.app}){extra}")
        if self.failed:
            lines.append("")
            lines.append("Use ci_failure_logs with a check name or job id to read a failed job's log.")
        return _clip("\n".join(lines), self.max_chars)

    async def logs_tool(self, args: dict[str, Any]) -> str:
        self.load()
        key = str(args.get("job") or "").strip()
        tail = int(args.get("tail_lines") or 200)
        pattern = str(args.get("grep") or "").strip()
        if not key:
            return "ERROR: pass a check name substring or a job id."
        candidates = [c for c in self.checks if c.is_actions and (str(c.id) == key or key.lower() in c.name.lower())]
        if not candidates:
            names = ", ".join(c.name for c in self.checks if c.is_actions)[:1500]
            return f"No GitHub Actions job matches {key!r}. Actions jobs on this commit: {names or 'none'}."
        failed_first = sorted(candidates, key=lambda c: c not in self.failed)
        check = failed_first[0]
        try:
            raw = self.gh.job_logs(check.id)
        except GitHubError as exc:
            return f"ERROR: could not fetch logs for job {check.id} ({check.name}): {exc}"
        lines = [_ANSI.sub("", _TIMESTAMP.sub("", ln)).rstrip() for ln in raw.splitlines()]
        header = f"# {check.name} [job {check.id}] {check.conclusion or check.status}; {len(lines)} log line(s)"
        if pattern:
            try:
                rx = re.compile(pattern, re.I)
            except re.error as exc:
                return f"ERROR: bad grep pattern: {exc}"
            keep: set[int] = set()
            matches = 0
            for i, ln in enumerate(lines):
                if rx.search(ln):
                    matches += 1
                    keep.update(range(max(0, i - 2), min(len(lines), i + 3)))
            picked = [lines[i] for i in sorted(keep)][-tail:]
            header += f"; {matches} line(s) matching {pattern!r} (with context)"
            body = picked
        else:
            body = lines[-tail:]
        if len(candidates) > 1:
            header += f"; {len(candidates) - 1} other job(s) also matched, pass the job id to pick one"
        return _clip(header + "\n" + "\n".join(body), self.max_chars)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n... [truncated to {n} characters; use grep or fewer tail_lines]"
