# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""OpenRouter chat completions with tool calling, model fallback and retries.

Model selection is OpenRouter's job: the request carries the ordered ``models``
list and OpenRouter routes to the first that accepts it. ``provider`` passes
routing preferences straight through (ordering, data-collection policy,
required parameters), see https://openrouter.ai/docs/features/provider-routing.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenRouterError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    requests: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        self.requests += 1
        if not usage:
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)
        self.cost += float(usage.get("cost") or 0.0)


@dataclass
class ChatResult:
    message: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return list(self.message.get("tool_calls") or [])

    @property
    def content(self) -> str:
        c = self.message.get("content")
        if isinstance(c, list):  # some providers return content parts
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c or ""


class OpenRouter:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        temperature: float = 0.1,
        max_tokens: int = 8000,
        provider: dict[str, Any] | None = None,
        timeout: float = 300.0,
        max_attempts: int = 5,
        client: httpx.Client | None = None,
    ):
        if not models:
            raise ValueError("at least one model is required")
        self.models = models
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.provider = provider
        self.max_attempts = max_attempts
        self.base_url = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/nashif/zephyr-reviewer",
            "X-Title": "zephyr-reviewer",
        }
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None:
            self._client.headers.update(headers)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.models[0],
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
        }
        if len(self.models) > 1:
            payload["models"] = self.models
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
            payload["parallel_tool_calls"] = True
        if self.provider:
            payload["provider"] = self.provider

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._client.post(f"{self.base_url}/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                self._sleep(attempt, None)
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_attempts:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                self._sleep(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.status_code >= 400:
                raise OpenRouterError(f"HTTP {resp.status_code}: {resp.text[:1000]}")
            data = resp.json()
            if "error" in data and not data.get("choices"):
                err = data["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                if code in RETRY_STATUSES and attempt < self.max_attempts:
                    last_error = msg
                    self._sleep(attempt, None)
                    continue
                raise OpenRouterError(f"OpenRouter error: {msg}")
            choices = data.get("choices") or []
            if not choices:
                raise OpenRouterError(f"no choices in response: {json.dumps(data)[:500]}")
            choice = choices[0]
            return ChatResult(
                message=choice.get("message") or {},
                model=data.get("model", payload["model"]),
                usage=data.get("usage") or {},
                finish_reason=choice.get("finish_reason") or "",
            )
        raise OpenRouterError(f"giving up after {self.max_attempts} attempts: {last_error}")

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None) -> None:
        delay = min(60.0, 2.0**attempt + random.uniform(0, 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(delay)


def assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields providers accept back; preserve reasoning blocks where OpenRouter returns them."""
    out: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    if message.get("reasoning_details"):
        out["reasoning_details"] = message["reasoning_details"]
    return out
