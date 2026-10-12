# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Existing PR discussion: fetching, digesting, threading, and use by the agent."""

from __future__ import annotations

import json

import httpx

from zephyr_reviewer.review.agent import LocalTool
from zephyr_reviewer.review.discussion import PR_DISCUSSION_TOOL, Discussion
from zephyr_reviewer.review.github_api import GitHub

MARKER = "<!-- zephyr-reviewer -->"


def _user(login, kind="User"):
    return {"login": login, "type": kind}


ISSUE_COMMENTS = [
    {
        "id": 1,
        "user": _user("alice"),
        "created_at": "2026-10-09T10:00:00Z",
        "body": "Please split the Kconfig change into its own commit.",
    },
    {
        "id": 2,
        "user": _user("zephyrbot"),
        "created_at": "2026-10-09T10:05:00Z",
        "body": "The following west manifest projects have changed revision...",
    },
    {"id": 3, "user": _user("github-actions[bot]", "Bot"), "created_at": "2026-10-09T10:06:00Z", "body": "CI failed."},
    {
        "id": 4,
        "user": _user("zephyrbot"),
        "created_at": "2026-10-09T11:00:00Z",
        "body": MARKER + "\n## AI review\nOld finding about a lock.",
    },
]
REVIEW_COMMENTS = [
    {
        "id": 10,
        "user": _user("bob"),
        "created_at": "2026-10-09T12:00:00Z",
        "path": "drivers/i2c/i2c_foo.c",
        "line": 17,
        "original_line": 17,
        "position": 5,
        "in_reply_to_id": None,
        "body": "This early return leaks the lock, doesn't it?",
    },
    {
        "id": 11,
        "user": _user("author"),
        "created_at": "2026-10-09T12:30:00Z",
        "path": "drivers/i2c/i2c_foo.c",
        "line": 17,
        "original_line": 17,
        "position": 5,
        "in_reply_to_id": 10,
        "body": "Good catch, will fix in the next push.",
    },
    {
        "id": 12,
        "user": _user("carol"),
        "created_at": "2026-10-08T09:00:00Z",
        "path": "drivers/i2c/Kconfig",
        "line": None,
        "original_line": 19,
        "position": None,
        "in_reply_to_id": None,
        "body": "Should this depend on I2C rather than select?",
    },
]
REVIEWS = [
    {
        "id": 100,
        "user": _user("bob"),
        "submitted_at": "2026-10-09T12:00:00Z",
        "state": "CHANGES_REQUESTED",
        "body": "Lock handling needs work.",
    },
    {"id": 101, "user": _user("alice"), "submitted_at": "2026-10-09T13:00:00Z", "state": "APPROVED", "body": ""},
    {"id": 102, "user": _user("bob"), "submitted_at": "2026-10-10T08:00:00Z", "state": "APPROVED", "body": "LGTM now."},
    {"id": 103, "user": _user("dave"), "submitted_at": "2026-10-10T09:00:00Z", "state": "COMMENTED", "body": "nit"},
    {"id": 104, "user": _user("erin"), "submitted_at": None, "state": "PENDING", "body": "draft"},
]


def _gh() -> GitHub:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/issues/7/comments"):
            return httpx.Response(200, json=ISSUE_COMMENTS)
        if p.endswith("/pulls/7/comments"):
            return httpx.Response(200, json=REVIEW_COMMENTS)
        if p.endswith("/pulls/7/reviews"):
            return httpx.Response(200, json=REVIEWS)
        return httpx.Response(404)

    return GitHub("tok", "o/r", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_fetch_separates_own_review_and_flags_bots():
    d = Discussion.fetch(_gh(), 7, MARKER)
    assert [c.author for c in d.conversation] == ["alice", "zephyrbot", "github-actions[bot]"]
    assert [c.is_bot for c in d.conversation] == [False, True, True]
    assert d.previous_ai is not None and "Old finding" in d.previous_ai.body
    assert [r.state for r in d.reviews] == ["CHANGES_REQUESTED", "APPROVED", "APPROVED", "COMMENTED"]  # PENDING dropped


def test_review_states_take_the_latest_per_reviewer():
    d = Discussion.fetch(_gh(), 7, MARKER)
    assert d.review_states() == {"alice": "APPROVED", "bob": "APPROVED"}


def test_threads_group_replies_and_mark_outdated():
    d = Discussion.fetch(_gh(), 7, MARKER)
    threads = d.threads()
    assert [(t[0].path, t[0].line, len(t)) for t in threads] == [
        ("drivers/i2c/Kconfig", 19, 1),
        ("drivers/i2c/i2c_foo.c", 17, 2),
    ]
    assert threads[0][0].outdated and not threads[1][0].outdated
    assert threads[1][1].author == "author"
    assert len(d.threads("drivers/i2c/i2c_foo.c")) == 1
    assert d.threads(include_outdated=False)[0][0].path == "drivers/i2c/i2c_foo.c"


def test_digest_is_bounded_and_points_at_the_tool():
    d = Discussion.fetch(_gh(), 7, MARKER)
    text = d.digest()
    assert text.startswith("3 conversation comment(s), 3 inline comment(s) in 2 thread(s), 4 review(s).")
    assert "Review states: @alice: APPROVED, @bob: APPROVED" in text
    assert "- @bob [CHANGES_REQUESTED] (2026-10-09): Lock handling needs work." in text
    assert "- @zephyrbot (bot) (2026-10-09): The following west manifest" in text
    assert "- drivers/i2c/Kconfig:19 @carol (2026-10-08) (outdated: the code moved since): Should this depend" in text
    assert "    - @author: Good catch" in text
    assert "previous automated review" in text and "Old finding about a lock." in text
    short = d.digest(max_chars=300)
    assert len(short) < 420 and "call `pr_discussion`" in short
    assert Discussion().digest() == ""


async def test_detail_filters_by_path_and_serves_as_a_tool():
    d = Discussion.fetch(_gh(), 7, MARKER)
    everything = await d.tool_handler({})
    assert (
        "## Review by @bob [CHANGES_REQUESTED]" in everything
        and "## Thread drivers/i2c/Kconfig:19 (outdated)" in everything
    )
    only_foo = await d.tool_handler({"path": "drivers/i2c/i2c_foo.c"})
    assert "Kconfig" not in only_foo and "leaks the lock" in only_foo and "Review by" not in only_foo
    assert "No discussion on nope.c" in await d.tool_handler({"path": "nope.c"})
    assert "No pull request discussion" in await Discussion().tool_handler({})
    tool = LocalTool(PR_DISCUSSION_TOOL, d.tool_handler)
    assert tool.name == "pr_discussion"


async def test_agent_offers_the_discussion_tool_and_digest(mini_repo):
    from tests.test_agent import GOOD_REVIEW, ScriptedOpenRouter, _completion, _llm, _tool_call
    from zephyr_reviewer.intel import SourceIntel
    from zephyr_reviewer.mcp_server import build_server
    from zephyr_reviewer.review.agent import run_review
    from zephyr_reviewer.review.github_api import PullRequest
    from zephyr_reviewer.review.mcp_client import ToolBridge
    from zephyr_reviewer.review.prompts import load_profile, opening_message, system_prompt

    d = Discussion.fetch(_gh(), 7, MARKER)
    review = dict(
        GOOD_REVIEW, discussion_assessment="@bob's lock concern at i2c_foo.c:17 is still unaddressed at this head."
    )
    script = ScriptedOpenRouter(
        [
            _completion(
                {"content": "", "tool_calls": [_tool_call("c1", "pr_discussion", {"path": "drivers/i2c/i2c_foo.c"})]}
            ),
            _completion({"content": "", "tool_calls": [_tool_call("c2", "submit_review", review)]}),
        ]
    )
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    pr = PullRequest(7, "t", "", "author", "main", mini_repo["base"], "pr", mini_repo["head"], "o/r", False)
    opening = opening_message(pr, intel.overview(), intel.diff_text, "", "o/r", d.digest())
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(
            _llm(script),
            bridge,
            system_prompt(load_profile("zephyr"), "English"),
            opening,
            {f.path: f.new_side_lines for f in intel.files},
            extra_tools=[LocalTool(PR_DISCUSSION_TOOL, d.tool_handler)],
            log=lambda m: None,
        )
    assert run.review is not None and run.tool_call_count == 1
    first, second = script.requests
    assert "pr_discussion" in {t["function"]["name"] for t in first["tools"]}
    assert "### Existing discussion on this pull request" in first["messages"][1]["content"]
    assert "@bob: APPROVED" in first["messages"][1]["content"]
    tool_msg = [m for m in second["messages"] if m["role"] == "tool"][0]
    assert tool_msg["tool_call_id"] == "c1" and "leaks the lock" in tool_msg["content"]
    assert run.review.discussion_assessment.startswith("@bob's lock concern")

    from zephyr_reviewer.review.render import render_comment

    body = render_comment(run.review, run, pr, repo="o/r", tag="zephyr-reviewer")
    assert "### Existing discussion" in body and "still unaddressed" in body
    assert json.dumps(run.review.to_dict())  # serializable for review.json
