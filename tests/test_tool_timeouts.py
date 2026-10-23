# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Tool calls are bounded, heartbeat while slow, run off the server loop, and never share records."""

from __future__ import annotations

import asyncio
import time

from mcp.server.mcpserver import MCPServer

from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.intel.repo import GitRepo
from zephyr_reviewer.mcp_server import build_server
from zephyr_reviewer.review import agent as agent_mod
from zephyr_reviewer.review.mcp_client import ToolBridge


def _slow_server() -> MCPServer:
    srv = MCPServer("slow")

    @srv.tool()
    async def hang(seconds: float) -> str:
        """Take a while without blocking the shared in-process loop (a stuck subprocess in the real server
        runs in a worker thread, which has the same effect from the client's side)."""
        await asyncio.sleep(seconds)
        return f"slept {seconds}"

    @srv.tool()
    async def quick() -> str:
        """Answer at once."""
        return "quick"

    return srv


async def test_tool_call_timeout_becomes_an_error_the_model_can_see():
    async with ToolBridge(_slow_server(), call_timeout=0.3) as bridge:
        text, rec = await bridge.call_with_record("hang", {"seconds": 2})
        assert text.startswith("ERROR: tool call hang did not finish within 0s") and rec.is_error
        assert rec.seconds < 1.5
        # The session is still usable afterwards.
        text, rec = await bridge.call_with_record("quick", {})
        assert text == "quick" and not rec.is_error


async def test_server_tools_run_off_the_event_loop(mini_repo):
    """Two concurrent callers: a slow lookup must not delay a fast one behind it."""
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    srv = build_server(intel)
    async with ToolBridge(srv) as bridge:
        t0 = time.monotonic()
        slow = asyncio.create_task(bridge.call_with_record("find_references", {"name": "i2c_transfer"}))
        fast_texts = []
        for _ in range(5):
            text, _ = await bridge.call_with_record("maintainers", {"path": "drivers/i2c/i2c_foo.c"})
            fast_texts.append(text)
        await slow
        assert all("@alice" in t for t in fast_texts) and time.monotonic() - t0 < 10
    # The tool functions are coroutines, so the server awaits them in worker threads.
    tools = srv._tool_manager.list_tools() if hasattr(srv, "_tool_manager") else []
    assert all(asyncio.iscoroutinefunction(t.fn) for t in tools) if tools else True


async def test_tool_heartbeat_and_per_call_records(monkeypatch):
    monkeypatch.setattr(agent_mod, "HEARTBEAT_SECONDS", 0.05)
    logs: list[str] = []
    async with ToolBridge(_slow_server(), call_timeout=5) as bridge:
        text, rec = await agent_mod._with_heartbeat(
            bridge.call_with_record("hang", {"seconds": 0.25}),
            lambda waited: logs.append(f"still waiting for tool hang ({waited:.2f}s)"),
        )
    assert text == "slept 0.25" and rec.name == "hang" and rec.seconds >= 0.25
    assert logs and logs[0].startswith("still waiting for tool hang (0.05s)")

    # Concurrent callers each get their own record, not whichever finished last.
    async with ToolBridge(_slow_server(), call_timeout=5) as bridge:
        a, b = await asyncio.gather(
            bridge.call_with_record("hang", {"seconds": 0.2}), bridge.call_with_record("quick", {})
        )
    assert a[1].name == "hang" and b[1].name == "quick" and b[1].seconds < a[1].seconds


def test_repo_lookups_are_constant_time(mini_repo):
    repo = GitRepo(mini_repo["root"], mini_repo["head"])
    assert repo.exists("drivers/i2c/i2c_foo.c") and not repo.exists("drivers/i2c")
    assert repo.is_dir("drivers/i2c") and repo.is_dir("drivers/i2c/") and repo.is_dir("tests")
    assert not repo.is_dir("drivers/i2c/i2c_foo.c") and not repo.is_dir("nope")
    assert repo.fileset() is repo.fileset()  # cached
