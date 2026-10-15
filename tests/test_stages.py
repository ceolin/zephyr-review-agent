# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Two-stage review: cheap model drafts, strong model verifies with the dossier and its own tools."""

from __future__ import annotations

import copy

from tests.test_agent import GOOD_REVIEW, ScriptedOpenRouter, _completion, _llm, _tool_call
from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.mcp_server import build_server
from zephyr_reviewer.review.github_api import PullRequest
from zephyr_reviewer.review.mcp_client import ToolBridge
from zephyr_reviewer.review.prompts import load_profile, opening_message, system_prompt
from zephyr_reviewer.review.render import render_comment
from zephyr_reviewer.review.stages import build_dossier, run_two_stage

DRAFT = copy.deepcopy(GOOD_REVIEW)
DRAFT["findings"] = DRAFT["findings"][:2] + [
    {
        "file": "drivers/i2c/i2c_foo.c",
        "line": 18,
        "severity": "medium",
        "category": "bug",
        "title": "Bogus draft finding",
        "problem": "The cheap model guessed.",
        "trigger": "",
        "impact": "",
        "recommendation": "",
        "confidence": "low",
    },
]
FINAL = copy.deepcopy(GOOD_REVIEW)
FINAL["findings"] = FINAL["findings"][:1]  # verifier keeps only the confirmed lock leak


async def _stage(mini_repo, draft_script, verify_script, **kw):
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(7, "t", "", "author", "main", mini_repo["base"], "pr", mini_repo["head"], "o/r", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", "o/r")
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_two_stage(
            _llm(draft_script, models=("cheap/model",)),
            _llm(verify_script, models=("strong/model", "strong/fallback")),
            bridge,
            system_prompt(load_profile("zephyr"), "English"),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            log=lambda m: None,
            **kw,
        )
    return run, pr


async def test_draft_then_verify_passes_evidence_and_merges_accounting(mini_repo):
    draft = ScriptedOpenRouter(
        [
            _completion(
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call(
                            "d1", "read_file", {"path": "drivers/i2c/i2c_foo.c", "start_line": 9, "end_line": 21}
                        ),
                        _tool_call("d2", "kconfig_symbol", {"name": "I2C_BUS_RECOVERY"}),
                        _tool_call(
                            "d3", "read_file", {"path": "drivers/i2c/i2c_foo.c", "start_line": 9, "end_line": 21}
                        ),  # dup
                        _tool_call("d4", "pr_overview", {}),
                    ],
                },
                prompt_tokens=500,
            ),
            _completion({"content": "", "tool_calls": [_tool_call("d5", "submit_review", DRAFT)]}, prompt_tokens=700),
        ]
    )
    verify = ScriptedOpenRouter(
        [
            _completion(
                {
                    "content": "",
                    "tool_calls": [
                        _tool_call(
                            "v1", "read_file", {"path": "drivers/i2c/i2c_foo.c", "start_line": 14, "end_line": 22}
                        ),
                    ],
                },
                prompt_tokens=3000,
            ),
            _completion({"content": "", "tool_calls": [_tool_call("v2", "submit_review", FINAL)]}, prompt_tokens=3200),
        ]
    )
    run, pr = await _stage(mini_repo, draft, verify, verify_max_steps=5)

    assert run.review is not None and len(run.review.findings) == 1
    assert run.steps == 4 and run.tool_call_count == 5
    assert run.models_used == ["anthropic/claude-sonnet-4.5"]  # the stub reports this slug for every call
    assert run.usage.requests == 4 and run.usage.prompt_tokens == 500 + 700 + 3000 + 3200
    assert [s["stage"] for s in run.stages] == ["draft", "verify"]
    assert run.stages[0]["steps"] == 2 and run.stages[0]["tool_calls"] == 4 and run.stages[0]["submitted"]
    assert run.stages[1]["usage"]["requests"] == 2
    assert {t.get("stage") for t in run.transcript} == {"draft", "verify"}

    # Stage 1 used the cheap model; stage 2 the strong one with its fallback.
    assert draft.requests[0]["model"] == "cheap/model" and "models" not in draft.requests[0]
    assert verify.requests[0]["models"] == ["strong/model", "strong/fallback"]

    # The verifier's opening carries the normal context, the draft, and the deduplicated dossier.
    v_open = verify.requests[0]["messages"][1]["content"]
    assert "### Full diff" in v_open and "## Draft review from a first-pass model" in v_open
    assert "#### D3. Bogus draft finding" in v_open
    assert v_open.count("### read_file(path='drivers/i2c/i2c_foo.c', start_line=9, end_line=21)") == 1
    assert "### kconfig_symbol(name='I2C_BUS_RECOVERY')" in v_open and "No Kconfig definition" in v_open
    assert "### pr_overview(" not in v_open  # already in the opening message
    assert "Do not mention the draft" in v_open
    # The verifier still had live tools, with the smaller budget.
    assert {t["function"]["name"] for t in verify.requests[0]["tools"]} >= {"read_file", "submit_review"}
    v_tool = [m for m in verify.requests[1]["messages"] if m["role"] == "tool"][0]
    assert "   17+| \tif (ret < 0) {" in v_tool["content"]

    assert "two stages" not in render_comment(run.review, run, pr, repo="o/r", tag="t")
    body = render_comment(run.review, run, pr, repo="o/r", tag="t", details=True)
    assert "in two stages, draft: anthropic/claude-sonnet-4.5 (2 step(s)) then verify:" in body
    assert "Bogus" not in body


async def test_verifier_budget_forces_submission(mini_repo):
    draft = ScriptedOpenRouter([_completion({"content": "", "tool_calls": [_tool_call("d", "submit_review", DRAFT)]})])
    verify = ScriptedOpenRouter(
        [
            _completion({"content": "", "tool_calls": [_tool_call("v1", "pr_overview", {})]}),
            _completion({"content": "", "tool_calls": [_tool_call("v2", "submit_review", FINAL)]}),
        ]
    )
    run, _ = await _stage(mini_repo, draft, verify, verify_max_steps=2)
    assert run.review is not None and run.forced
    assert verify.requests[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_review"}}


async def test_failed_draft_falls_back_to_a_full_single_stage_review(mini_repo):
    bad = _completion({"content": "", "tool_calls": [_tool_call("d", "submit_review", {"summary": "x"})]})
    draft = ScriptedOpenRouter([bad, bad, bad, bad])
    verify = ScriptedOpenRouter([_completion({"content": "", "tool_calls": [_tool_call("v", "submit_review", FINAL)]})])
    run, _ = await _stage(mini_repo, draft, verify, max_steps=30, verify_max_steps=3)
    assert run.review is not None
    assert not run.stages[0]["submitted"] and run.stages[1]["submitted"]
    assert any(e.startswith("draft stage:") for e in run.errors)
    v_open = verify.requests[0]["messages"][1]["content"]
    assert "Draft review" not in v_open  # plain opening: the verifier runs the whole review itself


def test_dossier_respects_budget_and_priority():
    results = [
        {"name": "search", "arguments": {"pattern": "x"}, "content": "s" * 100},
        {"name": "read_file", "arguments": {"path": "a.c"}, "content": "r" * 100},
        {"name": "read_file", "arguments": {"path": "a.c"}, "content": "r" * 100},
        {"name": "find_definition", "arguments": {"name": "f"}, "content": "d" * 100},
        {"name": "commit_messages", "arguments": {}, "content": "c" * 100},
    ]
    text = build_dossier(results, max_chars=330)
    heads = [line for line in text.splitlines() if line.startswith("### ")]
    assert heads == ["### read_file(path='a.c')", "### find_definition(name='f')"]
    assert "1 further stage-1 lookup(s) omitted" in text
    assert build_dossier([], 1000).startswith("(stage 1 recorded no reusable lookups)")
    clipped = build_dossier([{"name": "read_file", "arguments": {}, "content": "z" * 20}], 1000, per_result_chars=10)
    assert "clipped in dossier" in clipped
