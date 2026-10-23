# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Nothing waits silently: model calls are announced, heartbeat while outstanding, retries are logged,
and a wall-clock budget forces submission."""

from __future__ import annotations

import json
import time

import httpx

from tests.test_agent import GOOD_REVIEW, _completion, _tool_call
from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.mcp_server import build_server
from zephyr_reviewer.review import agent as agent_mod
from zephyr_reviewer.review.agent import run_review
from zephyr_reviewer.review.mcp_client import ToolBridge
from zephyr_reviewer.review.openrouter import OpenRouter, OpenRouterError


def _llm(handler, **kw) -> OpenRouter:
    llm = OpenRouter("k", ["m"], client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)
    llm._sleep = lambda *a: None  # type: ignore[method-assign]
    return llm


async def test_model_calls_are_announced_and_heartbeat_while_waiting(mini_repo, monkeypatch):
    monkeypatch.setattr(agent_mod, "HEARTBEAT_SECONDS", 0.05)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.2)  # a slow model answer: several heartbeats long
            return httpx.Response(
                200,
                json=_completion(
                    {"content": "", "tool_calls": [_tool_call("a", "maintainers", {"path": "drivers/i2c/i2c_foo.c"})]}
                ),
            )
        return httpx.Response(
            200, json=_completion({"content": "", "tool_calls": [_tool_call("z", "submit_review", GOOD_REVIEW)]})
        )

    logs: list[str] = []
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(
            _llm(handler, timeout=42),
            bridge,
            "sys",
            "open",
            {f.path: f.new_side_lines for f in intel.files},
            log=logs.append,
        )
    assert run.review is not None
    assert logs[0].startswith("[step 1] waiting for m (2 messages, ~")
    heartbeats = [m for m in logs if "still waiting for m" in m]
    assert heartbeats and "request timeout 42s" in heartbeats[0]
    assert any(m.startswith("[step 1] ") and "answered in" in m and "1 tool call(s)" in m for m in logs)
    assert any(m.startswith("[step 2] waiting for m (4 messages") for m in logs)


async def test_time_budget_forces_submission(mini_repo):
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        time.sleep(0.15)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json=_completion(
                    {"content": "", "tool_calls": [_tool_call("a", "maintainers", {"path": "drivers/i2c/i2c_foo.c"})]}
                ),
            )
        return httpx.Response(
            200, json=_completion({"content": "", "tool_calls": [_tool_call("z", "submit_review", GOOD_REVIEW)]})
        )

    logs: list[str] = []
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    async with ToolBridge(build_server(intel)) as bridge:
        run = await run_review(
            _llm(handler),
            bridge,
            "sys",
            "open",
            {f.path: f.new_side_lines for f in intel.files},
            max_seconds=0.1,
            log=logs.append,
        )
    assert run.review is not None and run.forced
    assert seen[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_review"}}
    assert any("time budget of 0s exhausted; forcing submit_review" in m for m in logs)


def test_retries_are_reported_with_reason_and_wait():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="overloaded", headers={"Retry-After": "7"})
        if calls["n"] == 2:
            raise httpx.ReadTimeout("read timed out")
        return httpx.Response(200, json=_completion({"content": "hi"}))

    events: list[str] = []
    llm = _llm(handler, max_attempts=4, timeout=90, on_event=events.append)
    result = llm.chat([{"role": "user", "content": "x"}])
    assert result.content == "hi"
    assert events[0].startswith("model request failed (HTTP 503: overloaded); retry 2/4 in ") and events[0].endswith(
        "s"
    )
    assert float(events[0].rsplit(" in ", 1)[1][:-1]) >= 7
    assert "no response within 90s (ReadTimeout)" in events[1] and "retry 3/4" in events[1]

    def always_timeout(request):
        raise httpx.ReadTimeout("read timed out")

    llm = _llm(always_timeout, max_attempts=2, timeout=5, on_event=events.append)
    try:
        llm.chat([{"role": "user", "content": "x"}])
    except OpenRouterError as exc:
        assert "giving up after 2 attempts: no response within 5s" in str(exc)
    else:
        raise AssertionError("expected OpenRouterError")
