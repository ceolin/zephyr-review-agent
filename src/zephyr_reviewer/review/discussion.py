# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""What has already been said on the pull request: conversation, inline review comments, review states.

The model gets a bounded digest up front and a ``pr_discussion`` tool for the full text, so it can
avoid repeating a maintainer, notice requests the head commit already addresses, and weigh the
author's explanations against the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .github_api import GitHub

PR_DISCUSSION_TOOL_NAME = "pr_discussion"

PR_DISCUSSION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PR_DISCUSSION_TOOL_NAME,
        "description": (
            "Full text of the existing pull request discussion: conversation comments, inline review "
            "comments grouped into threads (with file and line), and each reviewer's latest review state. "
            "Optionally filtered to one file. Use it when the digest in the opening message was truncated "
            "or when you need the exact wording of a thread."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "description": "Only threads on this file; empty for everything."},
                "include_outdated": {
                    "type": "boolean",
                    "description": "Include inline comments whose lines changed since they were written.",
                },
            },
        },
    },
}


@dataclass
class Comment:
    kind: str  # conversation | inline | review
    author: str
    created: str
    body: str
    is_bot: bool = False
    path: str | None = None
    line: int | None = None
    outdated: bool = False
    state: str | None = None
    id: int = 0
    in_reply_to: int | None = None
    url: str = ""

    @property
    def who(self) -> str:
        return f"@{self.author}" + (" (bot)" if self.is_bot else "")


@dataclass
class Discussion:
    conversation: list[Comment] = field(default_factory=list)
    inline: list[Comment] = field(default_factory=list)
    reviews: list[Comment] = field(default_factory=list)
    previous_ai: Comment | None = None

    @property
    def empty(self) -> bool:
        return not (self.conversation or self.inline or self.reviews or self.previous_ai)

    @classmethod
    def fetch(cls, gh: GitHub, number: int, own_marker: str) -> Discussion:
        d = cls()
        for raw in gh.issue_comments(number):
            c = _comment("conversation", raw)
            if own_marker and own_marker in c.body:
                d.previous_ai = c  # keep only the latest
                continue
            d.conversation.append(c)
        for raw in gh.review_comments(number):
            c = _comment("inline", raw)
            c.path = raw.get("path")
            c.line = raw.get("line") or raw.get("original_line")
            c.outdated = raw.get("position") is None
            c.in_reply_to = raw.get("in_reply_to_id")
            d.inline.append(c)
        for raw in gh.reviews(number):
            state = (raw.get("state") or "").upper()
            if state == "PENDING":
                continue
            c = _comment("review", raw, created_key="submitted_at")
            c.state = state
            if own_marker and own_marker in c.body:
                continue
            d.reviews.append(c)
        return d

    # ---------------------------------------------------------------- views
    def review_states(self) -> dict[str, str]:
        """Latest non-comment review state per reviewer."""
        latest: dict[str, tuple[str, str]] = {}
        for r in self.reviews:
            if r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                prev = latest.get(r.author)
                if prev is None or r.created >= prev[0]:
                    latest[r.author] = (r.created, r.state)
        return {who: state for who, (_, state) in sorted(latest.items())}

    def threads(self, path: str | None = None, include_outdated: bool = True) -> list[list[Comment]]:
        roots: dict[int, list[Comment]] = {}
        orphans: list[list[Comment]] = []
        for c in sorted(self.inline, key=lambda c: c.created):
            if path and c.path != path:
                continue
            if c.in_reply_to and c.in_reply_to in roots:
                roots[c.in_reply_to].append(c)
            elif c.in_reply_to:
                orphans.append([c])
            else:
                roots[c.id] = [c]
        out = [*roots.values(), *orphans]
        if not include_outdated:
            out = [t for t in out if not t[0].outdated]
        return sorted(out, key=lambda t: (t[0].path or "", t[0].line or 0, t[0].created))

    def digest(self, max_chars: int = 12_000, body_chars: int = 700) -> str:
        if self.empty:
            return ""
        lines = [
            f"{len(self.conversation)} conversation comment(s), {len(self.inline)} inline comment(s) in "
            f"{len(self.threads())} thread(s), {len(self.reviews)} review(s)."
        ]
        states = self.review_states()
        if states:
            lines.append("Review states: " + ", ".join(f"@{u}: {s}" for u, s in states.items()))
        substantive = [r for r in self.reviews if r.body.strip()]
        if substantive:
            lines += ["", "#### Review summaries"]
            for r in substantive:
                lines.append(f"- {r.who} [{r.state}] ({r.created[:10]}): {_clip(r.body, _budget(r, body_chars))}")
        if self.conversation:
            lines += ["", "#### Conversation"]
            for c in self.conversation:
                lines.append(f"- {c.who} ({c.created[:10]}): {_clip(c.body, _budget(c, body_chars))}")
        threads = self.threads()
        if threads:
            lines += ["", "#### Inline threads"]
            for t in threads:
                root = t[0]
                flag = " (outdated: the code moved since)" if root.outdated else ""
                lines.append(
                    f"- {root.path}:{root.line} {root.who} ({root.created[:10]}){flag}: {_clip(root.body, body_chars)}"
                )
                for reply in t[1:]:
                    lines.append(f"    - {reply.who}: {_clip(reply.body, body_chars // 2)}")
        if self.previous_ai:
            lines += [
                "",
                "#### Your previous automated review on an earlier commit (not evidence; re-verify everything)",
                _clip(self.previous_ai.body, 2500),
            ]
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [digest truncated; call `{PR_DISCUSSION_TOOL_NAME}` for the full text]"
        return text

    def detail(self, path: str | None = None, include_outdated: bool = True, max_chars: int = 24_000) -> str:
        if self.empty:
            return "No pull request discussion is available."
        lines: list[str] = []
        states = self.review_states()
        if states and not path:
            lines.append("Review states: " + ", ".join(f"@{u}: {s}" for u, s in states.items()))
        if not path:
            for r in self.reviews:
                if r.body.strip():
                    lines += [f"## Review by {r.who} [{r.state}] {r.created}", _clip(r.body, 4000), ""]
            for c in self.conversation:
                lines += [f"## Comment by {c.who} {c.created}", _clip(c.body, 4000), ""]
        for t in self.threads(path, include_outdated):
            root = t[0]
            flag = " (outdated)" if root.outdated else ""
            lines += [f"## Thread {root.path}:{root.line}{flag}"]
            for c in t:
                lines += [f"{c.who} {c.created}:", _clip(c.body, 3000), ""]
        if not lines:
            return f"No discussion on {path}." if path else "No pull request discussion is available."
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated to {max_chars} characters; filter by path]"
        return text

    async def tool_handler(self, args: dict[str, Any]) -> str:
        path = (args.get("path") or "").strip() or None
        return self.detail(path, include_outdated=bool(args.get("include_outdated", True)))


def _budget(c: Comment, body_chars: int) -> int:
    """Bots (CI, other reviewers) get a third of the space a person gets in the digest."""
    return max(120, body_chars // 3) if c.is_bot else body_chars


def _comment(kind: str, raw: dict[str, Any], created_key: str = "created_at") -> Comment:
    user = raw.get("user") or {}
    login = user.get("login") or "unknown"
    return Comment(
        kind=kind,
        author=login,
        created=raw.get(created_key) or raw.get("created_at") or "",
        body=(raw.get("body") or "").strip(),
        is_bot=(user.get("type") == "Bot") or login.lower().endswith("bot") or login.endswith("[bot]"),
        id=int(raw.get("id") or 0),
        url=raw.get("html_url") or "",
    )


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG = re.compile(
    r"<(?:picture|source|img|details|summary|br|p|div|span|sub|sup|b|i|strong|em)\b[^>]*>|</\w+>", re.I
)


def _plain(text: str) -> str:
    """Markdown-ish body without HTML scaffolding or layout whitespace, for one-line digests."""
    text = _HTML_COMMENT.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    return " ".join(text.split())


def _clip(text: str, n: int) -> str:
    text = _plain(text)
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."
