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
  already raised unless you add diagnosis or evidence. Use `discussion_assessment` only for a request
  the head commit does not address or a comment the code contradicts, with evidence; leave it empty
  otherwise. Author replies explain intent; check them against the code rather than deferring to
  them. A previous automated review is not evidence: re-verify anything it claimed. `pr_discussion`
  has the full text.
- Issues only. `summary` states the result and the key problem, not what the change does.
  `test_assessment`, `commit_message_assessment` and `discussion_assessment` are empty unless
  something is missing or wrong. Never write "tests are adequate" or "no issues found" in them.
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
    test_plan: bool = False,
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
    if test_plan:
        parts += ["", "### Test plan requested", TEST_PLAN_REQUEST]
    parts += ["", f"Investigate with the tools, then call `{SUBMIT_TOOL_NAME}`."]
    return "\n".join(p for p in parts if p is not None)


TEST_PLAN_REQUEST = """\
Besides the review, produce a `test_plan`: the twister runs that would exercise this change, with
platform coverage that matches what changed.

- Start from `test_candidates`: suites the PR edits, MAINTAINERS test identifiers, implicated tags,
  suites covering the changed source, and the platforms implicated by changed boards, SoCs,
  architectures and driver bindings. Read a suite's tests.yaml with `read_file` when you need
  scenario details.
- `suite` is the suite directory; `scenario` is one id from its tests.yaml (exact spelling), or empty
  for the whole suite. Use `list_platforms` to confirm every platform identifier you name.
- Cover the change, not the world: the platforms whose DTS instantiates a changed driver's compatible,
  the changed board itself, one emulation platform per touched architecture (native_sim, qemu_*),
  and the suite's own integration_platforms. Respect platform_allow and arch_allow.
- Mark `build_only` when compiling is the point (Kconfig, build system, board definitions).
- `must`: without it the change is unverified; `should`: covers a real risk; `optional`: breadth.
- At most 15 entries, most important first. Explain in `reason` what each run verifies.\
"""


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
