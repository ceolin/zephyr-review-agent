# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The false-positive guide and severity calibration reach every place findings are submitted or confirmed."""

from __future__ import annotations

import json

import httpx

from tests.test_agent import GOOD_REVIEW, ScriptedOpenRouter, _completion, _llm, _tool_call
from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.mcp_server import build_server
from zephyr_reviewer.review.github_api import PullRequest
from zephyr_reviewer.review.mcp_client import ToolBridge
from zephyr_reviewer.review.prompts import load_guides, load_profile, opening_message, system_prompt
from zephyr_reviewer.review.stages import run_two_stage

GUIDE_MARK = "# False-positive guide"
SEVERITY_MARK = "# Severity calibration"


def test_guides_are_operational_and_appended_to_the_single_agent_prompt():
    guides = load_guides()
    assert GUIDE_MARK in guides and SEVERITY_MARK in guides
    for rule in (
        "Defensive checks",
        "Unchecked return values",
        "Kconfig-guarded code",
        "Devicetree",
        "Kernel API semantics",
        "Pre-existing problems",
        "Claims in the commit message",
        "speculative",
    ):
        assert rule in guides
    assert "Never lower a level because you believe the path is unreachable" in guides
    with_guides = system_prompt(load_profile("zephyr"), "English")
    assert "# Before you submit" in with_guides and GUIDE_MARK in with_guides and SEVERITY_MARK in with_guides
    assert with_guides.index("# How to work") < with_guides.index("# Before you submit")
    without = system_prompt(load_profile("zephyr"), "English", include_guides=False)
    assert GUIDE_MARK not in without and "speculative: true" in without  # the working rules still name the flag
    # The profile no longer carries its own copy of the rules.
    assert "Review discipline" in load_profile("zephyr") and "Defensive checks" not in load_profile("zephyr")


async def test_two_stage_applies_guides_to_the_verifier_only(mini_repo):
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(7, "t", "", "a", "main", mini_repo["base"], "pr", mini_repo["head"], "o/r", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", "o/r")
    draft = ScriptedOpenRouter(
        [_completion({"content": "", "tool_calls": [_tool_call("d", "submit_review", GOOD_REVIEW)]})]
    )
    verify = ScriptedOpenRouter(
        [_completion({"content": "", "tool_calls": [_tool_call("v", "submit_review", GOOD_REVIEW)]})]
    )
    profile = load_profile("zephyr")
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_two_stage(
            _llm(draft, models=("cheap",)),
            _llm(verify, models=("strong",)),
            bridge,
            system_prompt(profile, "English", include_guides=False),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            verify_system=system_prompt(profile, "English", include_guides=True),
            verify_max_steps=3,
            log=lambda m: None,
        )
    assert run.review is not None
    assert GUIDE_MARK not in draft.requests[0]["messages"][0]["content"]
    assert GUIDE_MARK in verify.requests[0]["messages"][0]["content"]
    assert SEVERITY_MARK in verify.requests[0]["messages"][0]["content"]


async def test_pipeline_applies_guides_to_verification_and_hard_cases_not_specialists(mini_repo):
    from tests.test_pipeline import StageRouter
    from zephyr_reviewer.review.pipeline import hunk_text, run_pipeline

    router = StageRouter(verification_first_attempt_bad=False)
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(7, "t", "", "a", "main", mini_repo["base"], "pr", mini_repo["head"], "o/r", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", "o/r")
    llm = _llm(ScriptedOpenRouter([]), models=("m",))
    llm._client = httpx.Client(transport=httpx.MockTransport(router.handler))
    async with ToolBridge(build_server(intel)) as bridge:
        await run_pipeline(
            llm,
            llm,
            bridge,
            system_prompt(load_profile("zephyr"), "English", include_guides=False),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            changed_files=[f.path for f in intel.files],
            added_text=hunk_text(intel.diff_text),
            log=lambda m: None,
        )
    by_terminal: dict[str, list[str]] = {}
    for r in router.requests:
        terminal = next(t["function"]["name"] for t in r["tools"] if t["function"]["name"].startswith("submit"))
        by_terminal.setdefault(terminal, []).append(r["messages"][0]["content"])
    assert all(GUIDE_MARK not in s for s in by_terminal["submit_concerns"])
    assert all(GUIDE_MARK in s and SEVERITY_MARK in s for s in by_terminal["submit_verification"])
    assert all(GUIDE_MARK in s for s in by_terminal["submit_hard_case_verdict"])
    assert all(GUIDE_MARK not in s for s in by_terminal["submit_report"])  # findings are fixed by then
    # The verification stage prompt itself no longer duplicates the guide text.
    assert by_terminal["submit_verification"][0].count("Defensive checks") == 1


def test_review_command_single_mode_includes_guides(mini_repo, tmp_path, monkeypatch):
    import threading
    from http.server import HTTPServer

    from tests.test_cli_review import _Stub, stub_openrouter  # noqa: F401 - fixture import for pytest
    from zephyr_reviewer import cli

    _Stub.turns = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ZR_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test")
        rc = cli.main(
            [
                "review",
                "--repo",
                str(mini_repo["root"]),
                "--base",
                mini_repo["base"],
                "--head",
                mini_repo["head"],
                "--dry-run",
                "--in-process",
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        assert GUIDE_MARK in _Stub.turns[0]["messages"][0]["content"]
    finally:
        server.shutdown()
    _ = json  # keep the import used for readers tracing the stub format
