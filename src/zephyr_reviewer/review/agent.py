# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The review loop: model proposes tool calls, the bridge answers, until submit_review."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .mcp_client import ToolBridge
from .openrouter import OpenRouter, Usage, assistant_message_for_history
from .prompts import BUDGET_EXHAUSTED, NUDGE, rejection_message
from .schema import SUBMIT_REVIEW_TOOL, SUBMIT_TOOL_NAME, Review, parse_review


@dataclass
class LocalTool:
    """A tool answered by the orchestrator itself rather than the MCP server (e.g. PR discussion)."""

    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


@dataclass
class RunResult:
    review: Review | None
    steps: int
    usage: Usage
    models_used: list[str]
    forced: bool
    errors: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    tool_results: list[dict[str, Any]] = field(default_factory=list)  # name, arguments, content (for a later stage)
    stages: list[dict[str, Any]] = field(default_factory=list)  # per-stage summaries when the review ran in stages

    @property
    def tool_call_count(self) -> int:
        return sum(1 for t in self.transcript if t.get("kind") == "tool_call" and t.get("name") != SUBMIT_TOOL_NAME)

    def stage_summary(self, name: str) -> dict[str, Any]:
        return {
            "stage": name,
            "models": list(self.models_used),
            "steps": self.steps,
            "tool_calls": self.tool_call_count,
            "forced": self.forced,
            "submitted": self.review is not None,
            "seconds": round(self.seconds, 1),
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
                "cost": round(self.usage.cost, 4),
                "requests": self.usage.requests,
            },
        }


async def run_review(
    llm: OpenRouter,
    bridge: ToolBridge,
    system: str,
    opening: str,
    changed_lines: dict[str, set[int]],
    *,
    max_steps: int = 40,
    max_context_tokens: int = 160_000,
    max_rejections: int = 2,
    extra_tools: list[LocalTool] | None = None,
    log=print,
) -> RunResult:
    started = time.monotonic()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": opening}]
    local = {t.name: t for t in extra_tools or []}
    tools = [*bridge.tools, *(t.schema for t in local.values()), SUBMIT_REVIEW_TOOL]
    usage = Usage()
    models: list[str] = []
    transcript: list[dict[str, Any]] = []
    forced = False
    nudges = 0
    rejections = 0
    last_prompt_tokens = 0
    errors: list[str] = []
    tool_results: list[dict[str, Any]] = []

    for step in range(1, max_steps + 1):
        budget_out = step >= max_steps or last_prompt_tokens > max_context_tokens
        tool_choice: str | dict[str, Any] = "auto"
        if budget_out and not forced:
            forced = True
            messages.append({"role": "user", "content": BUDGET_EXHAUSTED})
        if forced:
            tool_choice = {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}}

        result = await asyncio.to_thread(llm.chat, messages, tools, tool_choice)
        usage.add(result.usage)
        last_prompt_tokens = int((result.usage or {}).get("prompt_tokens") or 0)
        if result.model and result.model not in models:
            models.append(result.model)
        messages.append(assistant_message_for_history(result.message))
        transcript.append(
            {
                "kind": "assistant",
                "step": step,
                "model": result.model,
                "content": result.content[:4000],
                "tool_calls": [tc.get("function", {}).get("name") for tc in result.tool_calls],
                "usage": result.usage,
            }
        )

        if not result.tool_calls:
            nudges += 1
            log(f"[step {step}] no tool call; nudging ({nudges})")
            if nudges > 2:
                forced = True
            messages.append({"role": "user", "content": NUDGE if not forced else BUDGET_EXHAUSTED})
            continue

        for tc in result.tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError as exc:
                args = None
                content = f"ERROR: arguments were not valid JSON ({exc}). Call the tool again with a JSON object."
            if args is not None:
                if name == SUBMIT_TOOL_NAME:
                    review, errs = parse_review(args, changed_lines)
                    transcript.append({"kind": "tool_call", "step": step, "name": name, "ok": not errs, "errors": errs})
                    if review is not None:
                        log(f"[step {step}] review submitted: {review.verdict}, {len(review.findings)} finding(s)")
                        return RunResult(
                            review,
                            step,
                            usage,
                            models,
                            forced,
                            errors,
                            transcript,
                            time.monotonic() - started,
                            tool_results,
                        )
                    rejections += 1
                    log(f"[step {step}] review rejected: {errs}")
                    if rejections > max_rejections:
                        errors.extend(errs)
                        return RunResult(
                            None,
                            step,
                            usage,
                            models,
                            forced,
                            errors,
                            transcript,
                            time.monotonic() - started,
                            tool_results,
                        )
                    content = rejection_message(errs)
                elif name in local:
                    t0 = time.monotonic()
                    try:
                        content = await local[name].handler(args)
                    except Exception as exc:  # noqa: BLE001 - report to the model, keep reviewing
                        content = f"ERROR: {type(exc).__name__}: {exc}"
                    log(f"[step {step}] {name}({_short(args)}) -> {len(content)} chars in {time.monotonic() - t0:.1f}s")
                    transcript.append(
                        {"kind": "tool_call", "step": step, "name": name, "arguments": args, "chars": len(content)}
                    )
                    tool_results.append({"name": name, "arguments": args, "content": content})
                elif name in bridge.tool_names:
                    content = await bridge.call(name, args)
                    rec = bridge.calls[-1]
                    log(
                        f"[step {step}] {name}({_short(args)}) -> {rec.result_chars} chars"
                        f"{' ERROR' if rec.is_error else ''} in {rec.seconds:.1f}s"
                    )
                    transcript.append(
                        {
                            "kind": "tool_call",
                            "step": step,
                            "name": name,
                            "arguments": args,
                            "chars": rec.result_chars,
                            "error": rec.is_error,
                        }
                    )
                    if not rec.is_error:
                        tool_results.append({"name": name, "arguments": args, "content": content})
                else:
                    available = ", ".join(sorted(bridge.tool_names | set(local)))
                    content = f"ERROR: unknown tool {name!r}. Available: {available}, {SUBMIT_TOOL_NAME}."
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "name": name, "content": content})

    errors.append(f"model did not submit a review within {max_steps} steps")
    return RunResult(
        None, max_steps, usage, models, forced, errors, transcript, time.monotonic() - started, tool_results
    )


def _short(args: dict[str, Any], limit: int = 80) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return s if len(s) <= limit else s[: limit - 3] + "..."
