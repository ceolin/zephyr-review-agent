# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Publish modes: post, ask (with confirmation and editing), never; and posting a saved review."""

from __future__ import annotations

import json
import os
import stat

import httpx

from tests.test_agent import GOOD_REVIEW
from zephyr_reviewer import cli
from zephyr_reviewer.review import publish as pub
from zephyr_reviewer.review.agent import RunResult
from zephyr_reviewer.review.config import ReviewConfig
from zephyr_reviewer.review.github_api import GitHub, PullRequest
from zephyr_reviewer.review.openrouter import Usage
from zephyr_reviewer.review.policy import Policy
from zephyr_reviewer.review.schema import Review, parse_review

CHANGED = {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}}


def _gh(seen: list) -> GitHub:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json={"html_url": "https://github.com/o/r/pull/7#pullrequestreview-1"})
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7#issuecomment-9"})

    return GitHub("tok", "o/r", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _run() -> tuple[RunResult, PullRequest]:
    review, _ = parse_review(GOOD_REVIEW, CHANGED)
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    return RunResult(review, 2, Usage(), ["m"], False), pr


def test_policy_publish_only_tightens():
    floor = Policy(publish="post")
    assert floor.tighten(publish="ask")[0].publish == "ask"
    assert floor.tighten(publish="never")[0].dry_run
    asked = Policy(publish="ask")
    p, rejected = asked.tighten(publish="post")
    assert p.publish == "ask" and rejected == ["publish=post is looser than the operator floor ask"]
    assert asked.tighten(publish="never")[0].publish == "never"


def test_config_publish_modes(monkeypatch):
    monkeypatch.setenv("INPUT_PUBLISH", "ask")
    assert ReviewConfig.from_env().publish == "ask"
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    cfg = ReviewConfig.from_env()
    assert cfg.publish == "never" and cfg.dry_run
    monkeypatch.setenv("INPUT_DRY_RUN", "false")
    monkeypatch.setenv("INPUT_PUBLISH", "maybe")
    assert any("publish must be one of" in e for e in ReviewConfig.from_env().validate())


def test_never_and_ask_without_terminal_do_not_post():
    run, pr = _run()
    seen: list = []
    logs: list[str] = []
    out = pub.publish(ReviewConfig(publish="never"), _gh(seen), pr, run, lambda e: "body", log=logs.append)
    assert out["posted"] == "false" and seen == []
    out = pub.publish(
        ReviewConfig(publish="ask", repo_slug="o/r"), _gh(seen), pr, run, lambda e: "body", log=logs.append
    )
    assert out["posted"] == "false" and seen == []
    assert any("zephyr-reviewer post --output-dir review-output --repository o/r" in m for m in logs)


def test_ask_posts_only_when_confirmed_and_uses_the_edited_body():
    run, pr = _run()
    seen: list = []
    asked: list = []

    def decline(body, n_inline):
        asked.append((body, n_inline))
        return False, body

    cfg = ReviewConfig(publish="ask", post_inline=True)
    out = pub.publish(cfg, _gh(seen), pr, run, lambda e: "original", log=lambda m: None, confirm=decline)
    assert out["posted"] == "false" and seen == [] and asked == [("original", 2)]

    def accept_edited(body, n_inline):
        return True, body + "\nedited by a human"

    out = pub.publish(cfg, _gh(seen), pr, run, lambda e: "original", log=lambda m: None, confirm=accept_edited)
    assert out["posted"] == "true" and out["comment_url"].endswith("issuecomment-9")
    posted = [b for m, p, b in seen if m == "POST" and p.endswith("/issues/7/comments")][0]
    assert posted["body"] == "original\nedited by a human"
    review_req = [b for m, p, b in seen if p.endswith("/reviews")][0]
    assert len(review_req["comments"]) == 2 and review_req["commit_id"] == "h" * 40


def test_post_saved_review_round_trip(tmp_path):
    run, pr = _run()
    cfg = ReviewConfig(repo_slug="o/r", output_dir=str(tmp_path / "out"), comment_tag="zephyr-reviewer")
    from zephyr_reviewer.review.render import inline_comments

    pub.save_artifacts(
        cfg.output_dir, run, "<!-- zephyr-reviewer -->\nsaved body", pr, cfg, inline_comments(run.review)
    )
    record, body = pub.load_saved(cfg.output_dir)
    assert record["pr"] == 7 and record["repo"] == "o/r" and body.endswith("saved body")
    assert Review.from_dict(record["review"]).findings[0].inline  # inline anchoring survives the round trip

    # A human edits the comment, then posts with inline comments.
    (tmp_path / "out" / "comment.md").write_text("reviewed and trimmed by a maintainer", encoding="utf-8")
    seen: list = []
    out = pub.post_saved(cfg.output_dir, _gh(seen), inline=True, tag="zephyr-reviewer", log=lambda m: None)
    assert out["posted"] == "true"
    posted = [b for m, p, b in seen if m == "POST" and p.endswith("/issues/7/comments")][0]
    assert posted["body"] == "<!-- zephyr-reviewer -->\nreviewed and trimmed by a maintainer"  # marker restored
    assert len([b for m, p, b in seen if p.endswith("/reviews")][0]["comments"]) == 2

    declined = pub.post_saved(
        cfg.output_dir, _gh([]), inline=False, tag="t", log=lambda m: None, confirm=lambda b, n: (False, b)
    )
    assert declined == {"posted": "false"}


def test_interactive_confirm_answers_and_edit(tmp_path, monkeypatch, capsys):
    comment = tmp_path / "comment.md"
    comment.write_text("draft\n", encoding="utf-8")
    pr = PullRequest(7, "", "", "", "", "", "", "h" * 40, "", False)
    confirm = cli._interactive_confirm(str(tmp_path), pr)

    prompts: list[str] = []
    answers = iter(["y"])

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    assert confirm("draft", 2) == (True, "draft")
    assert prompts[-1].startswith("Post the summary comment and 2 inline comment(s) to PR #7?")

    answers = iter([""])
    assert confirm("draft", 0) == (False, "draft")

    # 'e' runs $EDITOR on comment.md, re-reads it, and asks again.
    editor = tmp_path / "ed.sh"
    editor.write_text("#!/bin/sh\necho 'edited' >> \"$1\"\n", encoding="utf-8")
    editor.chmod(editor.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    answers = iter(["e", "y"])
    ok, body = confirm("draft", 0)
    assert ok and body == "draft\nedited\n"
    assert prompts[-1].startswith("Post the summary comment to PR #7?")
    assert "draft\nedited" in capsys.readouterr().err  # the edited comment is shown again before asking

    answers = iter(["what", "n"])
    assert confirm("draft", 0) == (False, "draft")


def test_post_command_requires_a_way_to_confirm(tmp_path, monkeypatch):
    run, pr = _run()
    cfg = ReviewConfig(repo_slug="o/r", output_dir=str(tmp_path / "out"))
    pub.save_artifacts(cfg.output_dir, run, "body", pr, cfg)
    monkeypatch.setattr(cli, "_has_terminal", lambda: False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert cli.main(["post", "--output-dir", cfg.output_dir]) == 2  # no terminal, no --yes
    os.environ.pop("GITHUB_TOKEN")
    assert cli.main(["post", "--output-dir", cfg.output_dir, "--yes"]) == 2  # no token
