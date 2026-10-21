# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""The real `review` command end to end: scripted OpenRouter over HTTP, in-process MCP server, dry run."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.test_agent import GOOD_REVIEW
from zephyr_reviewer import cli


class _Stub(BaseHTTPRequestHandler):
    turns: list[dict] = []

    def log_message(self, *a):  # noqa: D401 - silence
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.turns.append(body)
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            calls = [
                {
                    "id": "a",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "drivers/i2c/i2c_foo.c", "start_line": 9, "end_line": 21}),
                    },
                }
            ]
        else:
            calls = [
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "submit_review", "arguments": json.dumps(GOOD_REVIEW)},
                }
            ]
        resp = {
            "id": "x",
            "model": "stub/model",
            "choices": [
                {"message": {"role": "assistant", "content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub_openrouter():
    _Stub.turns = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_review_command_dry_run_writes_artifacts_and_prints_comment(
    mini_repo, stub_openrouter, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ZR_BASE_URL", stub_openrouter)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    out_dir = tmp_path / "out"
    rc = cli.main(
        [
            "review",
            "--repo",
            str(mini_repo["root"]),
            "--base",
            mini_repo["base"],
            "--head",
            mini_repo["head"],
            "--dry-run",
            "--in-process",
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0
    assert len(_Stub.turns) == 2
    printed = capsys.readouterr().out
    assert "## 🤖 AI review (advisory)" in printed and "🛑 **Request changes**" in printed
    # Inline delivery is the default: anchored findings are counted, unanchored ones listed.
    assert "2 finding(s) are posted as inline comments" in printed and "Unrelated file cited" in printed
    assert (out_dir / "comment.md").exists() and (out_dir / "transcript.jsonl").exists()
    inline = json.loads((out_dir / "inline_comments.json").read_text())
    assert [c["path"] for c in inline] == ["drivers/i2c/i2c_foo.c", "drivers/i2c/Kconfig"]
    record = json.loads((out_dir / "review.json").read_text())
    assert record["review"]["verdict"] == "request_changes" and record["head_sha"] == mini_repo["head"]


def test_review_command_no_inline_lists_everything(mini_repo, stub_openrouter, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZR_BASE_URL", stub_openrouter)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    rc = cli.main(
        [
            "review",
            "--repo",
            str(mini_repo["root"]),
            "--base",
            mini_repo["base"],
            "--head",
            mini_repo["head"],
            "--dry-run",
            "--in-process",
            "--no-inline",
            "--output-dir",
            str(tmp_path / "out"),
            "--fail-on-severity",
            "high",
        ]
    )
    assert rc == 1  # the gate fails on the high finding
    printed = capsys.readouterr().out
    assert "Early return leaks the bus lock" in printed and "posted as inline comments" not in printed
    assert json.loads((tmp_path / "out" / "inline_comments.json").read_text()) == []
