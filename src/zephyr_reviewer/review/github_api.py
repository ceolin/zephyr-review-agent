# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Minimal GitHub REST client: pull request metadata, merge base, comments, reviews."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class GitHubError(RuntimeError):
    pass


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    head_repo: str
    draft: bool
    labels: list[str] = field(default_factory=list)
    url: str = ""
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> PullRequest:
        head = d.get("head") or {}
        base = d.get("base") or {}
        return cls(
            number=int(d["number"]),
            title=d.get("title") or "",
            body=d.get("body") or "",
            author=(d.get("user") or {}).get("login", ""),
            base_ref=base.get("ref", ""),
            base_sha=base.get("sha", ""),
            head_ref=head.get("ref", ""),
            head_sha=head.get("sha", ""),
            head_repo=((head.get("repo") or {}).get("full_name") or ""),
            draft=bool(d.get("draft")),
            labels=[lab.get("name", "") for lab in d.get("labels") or []],
            url=d.get("html_url", ""),
            changed_files=int(d.get("changed_files") or 0),
            additions=int(d.get("additions") or 0),
            deletions=int(d.get("deletions") or 0),
        )


class GitHub:
    def __init__(
        self, token: str, repo: str, api_url: str = "https://api.github.com", client: httpx.Client | None = None
    ):
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "zephyr-reviewer",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(timeout=60.0, headers=headers)
        if client is not None:
            self._client.headers.update(headers)

    def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        for attempt in range(1, 5):
            try:
                resp = self._client.request(method, url, json=json_body, params=params)
            except httpx.HTTPError as exc:
                if attempt == 4:
                    raise GitHubError(f"{method} {path}: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(float(resp.headers.get("Retry-After", 2**attempt)))
                continue
            if resp.status_code >= 400:
                raise GitHubError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp
        raise GitHubError(f"{method} {path}: retries exhausted")

    def pull(self, number: int) -> PullRequest:
        return PullRequest.from_api(self._request("GET", f"/repos/{self.repo}/pulls/{number}").json())

    def merge_base(self, base: str, head: str) -> str:
        data = self._request("GET", f"/repos/{self.repo}/compare/{base}...{head}").json()
        return ((data.get("merge_base_commit") or {}).get("sha")) or ""

    def _paginate(self, path: str, max_pages: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = f"{self.api_url}{path}"
        params: dict | None = {"per_page": 100}
        for _ in range(max_pages):
            if not url:
                break
            resp = self._request("GET", url, params=params)
            params = None
            out.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
        return out

    def find_comment(self, number: int, marker: str) -> dict[str, Any] | None:
        for c in self._paginate(f"/repos/{self.repo}/issues/{number}/comments"):
            if marker in (c.get("body") or ""):
                return c
        return None

    def upsert_comment(self, number: int, body: str, marker: str) -> str:
        existing = self.find_comment(number, marker)
        if existing:
            resp = self._request(
                "PATCH", f"/repos/{self.repo}/issues/comments/{existing['id']}", json_body={"body": body}
            )
        else:
            resp = self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", json_body={"body": body})
        return resp.json().get("html_url", "")

    def create_review(self, number: int, commit_sha: str, comments: list[dict[str, Any]], body: str = "") -> str:
        payload = {"commit_id": commit_sha, "event": "COMMENT", "body": body, "comments": comments}
        resp = self._request("POST", f"/repos/{self.repo}/pulls/{number}/reviews", json_body=payload)
        return resp.json().get("html_url", "")


def file_url(repo: str, sha: str, path: str, line: int | None = None, api_url: str = "https://api.github.com") -> str:
    host = "https://github.com" if "api.github.com" in api_url else api_url.split("/api")[0]
    url = f"{host}/{repo}/blob/{sha}/{path}"
    return f"{url}#L{line}" if line else url


def event_payload() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pr_number_from_event() -> int | None:
    ev = event_payload()
    pr = ev.get("pull_request") or (ev.get("issue") or {})
    num = pr.get("number") if isinstance(pr, dict) else None
    return int(num) if num else None
