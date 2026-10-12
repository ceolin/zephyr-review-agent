# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Deliver the review: PR comment, inline review, action outputs, artifacts."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from .agent import RunResult
from .config import SEVERITY_RANK, ReviewConfig
from .github_api import GitHub, PullRequest
from .render import inline_comments, marker


def write_outputs(values: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in values.items():
            fh.write(f"{k}={v}\n")


def write_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")


def save_artifacts(out_dir: str, run: RunResult, body: str, pr: PullRequest, cfg: ReviewConfig) -> Path:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comment.md").write_text(body, encoding="utf-8")
    record = {
        "pr": pr.number,
        "head_sha": pr.head_sha,
        "base_sha": pr.base_sha,
        "repo": cfg.repo_slug,
        "models": run.models_used,
        "steps": run.steps,
        "forced": run.forced,
        "seconds": round(run.seconds, 1),
        "usage": dataclasses.asdict(run.usage),
        "errors": run.errors,
        "review": run.review.to_dict() if run.review else None,
    }
    (d / "review.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    with (d / "transcript.jsonl").open("w", encoding="utf-8") as fh:
        for entry in run.transcript:
            fh.write(json.dumps(entry) + "\n")
    return d


def gate_fails(run: RunResult, cfg: ReviewConfig) -> str | None:
    if run.review is None:
        return "review did not complete"
    threshold = SEVERITY_RANK.get(cfg.fail_on_severity, 0)
    if threshold and SEVERITY_RANK.get(run.review.highest_severity(), 0) >= threshold:
        return f"finding at severity {run.review.highest_severity()} (threshold {cfg.fail_on_severity})"
    return None


def publish(
    cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest, run: RunResult, body: str, log=print
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "verdict": run.review.verdict if run.review else "error",
        "findings_count": len(run.review.findings) if run.review else 0,
        "highest_severity": run.review.highest_severity() if run.review else "none",
        "comment_url": "",
        "review_url": "",
        "skipped": "false",
    }
    if cfg.dry_run or gh is None or not pr.number:
        log("dry run: not posting to GitHub")
        return outputs
    if cfg.post_comment:
        outputs["comment_url"] = gh.upsert_comment(pr.number, body, marker(cfg.comment_tag))
        log(f"comment: {outputs['comment_url']}")
    if cfg.post_inline and run.review is not None:
        comments = inline_comments(run.review)
        if comments:
            try:
                outputs["review_url"] = gh.create_review(
                    pr.number, pr.head_sha, comments, body=f"{len(comments)} inline note(s) from the AI review."
                )
                log(f"inline review: {outputs['review_url']} ({len(comments)} comment(s))")
            except Exception as exc:  # noqa: BLE001 - inline anchoring can be rejected; the summary already landed
                log(f"::warning::inline review rejected: {exc}")
    return outputs
