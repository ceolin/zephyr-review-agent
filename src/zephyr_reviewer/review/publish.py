# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Deliver the review: PR comment, inline review, action outputs, artifacts."""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import RunResult
from .config import SEVERITY_RANK, ReviewConfig
from .github_api import GitHub, PullRequest
from .render import inline_comments, marker
from .schema import Review

# Called in "ask" mode with the rendered comment and the number of inline comments; returns
# (post it?, body to post). The CLI provides an interactive one; a non-interactive job gets none.
ConfirmFn = Callable[[str, int], tuple[bool, str]]


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
        "stages": run.stages,
        "errors": run.errors,
        "review": run.review.to_dict() if run.review else None,
    }
    (d / "review.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if run.review is not None and run.review.test_plan:
        from .testplan import to_yaml

        (d / "test_plan.yaml").write_text(to_yaml(run.review, pr.head_sha), encoding="utf-8")
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
    cfg: ReviewConfig,
    gh: GitHub | None,
    pr: PullRequest,
    run: RunResult,
    body: str,
    log=print,
    confirm: ConfirmFn | None = None,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "verdict": run.review.verdict if run.review else "error",
        "findings_count": len(run.review.findings) if run.review else 0,
        "highest_severity": run.review.highest_severity() if run.review else "none",
        "test_plan_count": len(run.review.test_plan) if run.review else 0,
        "comment_url": "",
        "review_url": "",
        "posted": "false",
        "skipped": "false",
    }
    if gh is None or not pr.number:
        log("no pull request: nothing to post")
        return outputs
    if cfg.publish == "never":
        log("publish=never: not posting to GitHub")
        return outputs
    inline = inline_comments(run.review) if (cfg.post_inline and run.review is not None) else []
    if cfg.publish == "ask":
        if confirm is None:
            log(
                f"::notice::publish=ask without a terminal: the review was saved, not posted. Read it, then publish "
                f"with: zephyr-reviewer post --output-dir {cfg.output_dir} --repository {cfg.repo_slug}"
            )
            return outputs
        ok, body = confirm(body, len(inline))
        if not ok:
            log("not posted")
            return outputs
    posted = _post(gh, pr.number, pr.head_sha, body, inline, cfg.post_comment, cfg.comment_tag, log)
    outputs.update(posted)
    return outputs


def _post(
    gh: GitHub, number: int, head_sha: str, body: str, inline: list[dict[str, Any]], post_comment: bool, tag: str, log
) -> dict[str, Any]:
    out: dict[str, Any] = {"comment_url": "", "review_url": "", "posted": "false"}
    if post_comment:
        out["comment_url"] = gh.upsert_comment(number, body, marker(tag))
        out["posted"] = "true"
        log(f"comment: {out['comment_url']}")
    if inline:
        try:
            out["review_url"] = gh.create_review(
                number, head_sha, inline, body=f"{len(inline)} inline note(s) from the AI review."
            )
            out["posted"] = "true"
            log(f"inline review: {out['review_url']} ({len(inline)} comment(s))")
        except Exception as exc:  # noqa: BLE001 - inline anchoring can be rejected; the summary already landed
            log(f"::warning::inline review rejected: {exc}")
    return out


def load_saved(output_dir: str) -> tuple[dict[str, Any], str]:
    """The record and comment a previous run saved with save_artifacts."""
    d = Path(output_dir)
    record = json.loads((d / "review.json").read_text(encoding="utf-8"))
    body = (d / "comment.md").read_text(encoding="utf-8")
    return record, body


def post_saved(
    output_dir: str,
    gh: GitHub,
    *,
    inline: bool,
    tag: str,
    log=print,
    confirm: ConfirmFn | None = None,
) -> dict[str, Any]:
    """Publish a saved review (possibly edited in comment.md) to the pull request it was written for."""
    record, body = load_saved(output_dir)
    number = int(record.get("pr") or 0)
    if not number:
        raise SystemExit(f"{output_dir}/review.json does not belong to a pull request")
    if marker(tag) not in body:
        body = marker(tag) + "\n" + body
    review = Review.from_dict(record["review"]) if record.get("review") else None
    comments = inline_comments(review) if (inline and review is not None) else []
    if confirm is not None:
        ok, body = confirm(body, len(comments))
        if not ok:
            log("not posted")
            return {"posted": "false"}
    return _post(gh, number, record.get("head_sha") or "", body, comments, True, tag, log)
