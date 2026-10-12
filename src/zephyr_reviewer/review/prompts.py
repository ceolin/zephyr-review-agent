# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Prompt assembly: profile, working rules, and the opening message."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from .github_api import PullRequest
from .schema import SUBMIT_TOOL_NAME

WORKING_RULES = """\

---

# How to work

You are driving a set of tools over the repository at the pull request's head commit. Investigate
first, then submit. Concretely:

- Begin with `pr_overview` and `commit_messages`.
- Read every changed file's relevant region with `read_file`; the diff alone is not enough. Lines
  the PR added are marked with `+` in `read_file` output.
- Verify every assumption a finding rests on with a lookup, and record the lookups in `evidence`.
- Tool results are bounded in size and say so when truncated; narrow the query rather than guess.
- Read the existing discussion before forming findings. Do not re-report a concern a reviewer has
  already raised unless you add diagnosis or evidence; if the head commit addresses a request, or a
  comment is contradicted by the code, say so in `discussion_assessment` with evidence. Author
  replies explain intent; check them against the code rather than deferring to them. A previous
  automated review is not evidence: re-verify anything it claimed. `pr_discussion` has the full text.
- You have a step budget. When told the budget is exhausted, call `{submit}` immediately with what
  you have, lowering `confidence` where you could not verify.
- Finish by calling `{submit}` exactly once. Do not write the review as prose; the tool call is the
  review. `line` must be a line on the new side of the diff, or 0 for findings that are not anchored
  to one changed line (architecture, missing tests, cross-file behavior); those appear in the summary
  only. Write all prose in {language}.\
"""


def load_profile(name_or_path: str) -> str:
    p = Path(name_or_path)
    if p.suffix == ".md" and p.exists():
        return p.read_text(encoding="utf-8")
    try:
        return (
            resources.files("zephyr_reviewer.review")
            .joinpath(f"profiles/{name_or_path}.md")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        available = sorted(
            f.name[:-3]
            for f in resources.files("zephyr_reviewer.review").joinpath("profiles").iterdir()
            if f.name.endswith(".md")
        )
        raise SystemExit(
            f"unknown review profile {name_or_path!r}; available: {', '.join(available)}, or a path to a .md file"
        ) from None


def system_prompt(profile_text: str, language: str) -> str:
    return profile_text.rstrip() + "\n" + WORKING_RULES.format(submit=SUBMIT_TOOL_NAME, language=language)


def opening_message(
    pr: PullRequest,
    overview: str,
    diff: str | None,
    project_context: str,
    repo: str,
    discussion: str | None = None,
) -> str:
    parts = [
        f"## Pull request #{pr.number}: {pr.title}" if pr.number else f"## Change under review: {pr.title}",
        f"Repository: `{repo or 'local'}`   base `{pr.base_ref}` ({pr.base_sha[:12]}) "
        f"<- head `{pr.head_ref}` ({pr.head_sha[:12]})",
        f"Author: {pr.author}" if pr.author else "",
    ]
    if pr.body.strip():
        parts += ["", "### Author's description", pr.body.strip()[:6000]]
    if project_context.strip():
        parts += ["", "### Maintainer's review guidance", project_context.strip()[:6000]]
    parts += ["", "### Overview (from pr_overview)", overview]
    if discussion:
        parts += ["", "### Existing discussion on this pull request", discussion]
    if diff is not None:
        parts += ["", "### Full diff", "```diff", diff, "```"]
    else:
        parts += ["", "The diff is too large to include here; use `file_diff` per file."]
    parts += ["", f"Investigate with the tools, then call `{SUBMIT_TOOL_NAME}`."]
    return "\n".join(p for p in parts if p is not None)


NUDGE = (
    f"You replied with prose instead of a tool call. Continue investigating with the tools, or call "
    f"`{SUBMIT_TOOL_NAME}` to finish."
)

BUDGET_EXHAUSTED = (
    f"The step budget is exhausted. Call `{SUBMIT_TOOL_NAME}` now with the findings you have verified so far. "
    "Lower `confidence` on anything you could not check, and drop findings you could not support."
)


def rejection_message(errors: list[str]) -> str:
    return (
        "The review was rejected for these reasons; fix them and call "
        + SUBMIT_TOOL_NAME
        + " again:\n- "
        + "\n- ".join(errors)
    )
