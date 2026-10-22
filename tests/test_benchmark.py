# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Benchmark: mining fix history, reviewing culprits, judging, and reporting."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.conftest import MAINTAINERS
from zephyr_reviewer import cli
from zephyr_reviewer.benchmark import mine as bm
from zephyr_reviewer.benchmark.judge import findings_digest, judge
from zephyr_reviewer.benchmark.run import render_summary
from zephyr_reviewer.review.openrouter import OpenRouter


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(root: Path, msg: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-F", "-", input=msg) if False else subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-F", "-"], input=msg, text=True, check=True
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def history(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("hist")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    base = _commit(
        root,
        "Initial tree\n",
        {
            "MAINTAINERS.yml": MAINTAINERS,
            "drivers/i2c/i2c_foo.c": "int foo(void)\n{\n\treturn 0;\n}\n",
            "kernel/sem.c": "int take(void)\n{\n\tlock();\n\treturn 0;\n}\n",
            "doc/x.rst": "x\n",
        },
    )
    # Culprit 1: adds a leak; later fixed with a Fixes: trailer.
    c1 = _commit(
        root,
        "drivers: i2c: foo: add transfer\n\nAdd transfer.\n\nSigned-off-by: T <t@e>\n",
        {
            "drivers/i2c/i2c_foo.c": "int foo(void)\n{\n\tlock();\n\tif (bad()) {\n\t\treturn -EIO;\n\t}\n"
            "\tunlock();\n\treturn 0;\n}\n",
        },
    )
    # Culprit 2: a kernel change; later fixed with only an issue reference (needs blame).
    c2 = _commit(
        root,
        "kernel: sem: speed up take\n\nSigned-off-by: T <t@e>\n",
        {
            "kernel/sem.c": "int take(void)\n{\n\tfast_path();\n\tfast_path_2();\n\treturn 0;\n}\n",
        },
    )
    # A docs-only change: never a candidate culprit.
    _commit(root, "doc: tweak\n\nSigned-off-by: T <t@e>\n", {"doc/x.rst": "y\n"})
    f1 = _commit(
        root,
        "drivers: i2c: foo: release the lock on error\n\nThe early return leaks the bus lock when bad() fails, "
        "so every later\ntransfer blocks forever.\n\n"
        f'Fixes: {c1[:12]} ("drivers: i2c: foo: add transfer")\n\nFixes #4242\n\nSigned-off-by: T <t@e>\n',
        {
            "drivers/i2c/i2c_foo.c": "int foo(void)\n{\n\tlock();\n\tif (bad()) {\n\t\tunlock();\n\t\treturn -EIO;\n"
            "\t}\n\tunlock();\n\treturn 0;\n}\n"
        },
    )
    f2 = _commit(
        root,
        "kernel: sem: fix lost wakeup in fast path\n\nfast_path() skips the wait queue check, so a waiter can be lost."
        "\n\nFixes #777\nSigned-off-by: T <t@e>\n",
        {"kernel/sem.c": "int take(void)\n{\n\tlock();\n\treturn 0;\n}\n"},
    )
    # A fix commit whose removed lines come from the initial tree (root commit): not reviewable, skipped.
    _commit(root, "doc: fix typo\n\nFixes #1\nSigned-off-by: T <t@e>\n", {"doc/x.rst": "z\n"})
    return {"root": root, "base": base, "c1": c1, "c2": c2, "f1": f1, "f2": f2}


def test_mine_finds_trailer_and_blame_culprits(history):
    root = history["root"]
    entries = bm.mine(str(root), "HEAD", log=lambda m: None)
    by = {e.commit: e for e in entries}
    assert set(by) == {history["c1"], history["c2"]}
    e1 = by[history["c1"]]
    assert e1.source == "trailer" and e1.confidence == 1.0 and e1.fixed_by == history["f1"]
    assert e1.subsystem == "Drivers: I2C" and e1.files == ["drivers/i2c/i2c_foo.c"]
    assert e1.problem_description == (
        "drivers: i2c: foo: release the lock on error. The early return leaks the bus lock when bad() fails, "
        "so every later transfer blocks forever."
    )
    assert "Fixes" not in e1.problem_description and "Signed-off" not in e1.problem_description
    e2 = by[history["c2"]]
    assert e2.source == "blame" and e2.confidence == 1.0 and e2.extra == {"blamed_lines": 2}
    assert e2.subsystem == "Kernel" and e2.fix_subject.startswith("kernel: sem: fix lost wakeup")
    # Trailers only: the blame-derived entry disappears.
    assert [e.commit for e in bm.mine(str(root), "HEAD", use_blame=False)] == [history["c1"]]
    # Round trip through JSON.
    out = root / "bench.json"
    bm.save(entries, str(out))
    loaded = bm.load(str(out))
    assert [e.to_dict() for e in loaded] == [e.to_dict() for e in entries]
    # Datasets may be hand-written with only the essential keys.
    (root / "manual.json").write_text(json.dumps([{"commit": history["c1"], "problem_description": "leak"}]))
    m = bm.load(str(root / "manual.json"))[0]
    assert m.source == "manual" and m.confidence == 1.0 and m.files == []


def test_culprit_by_blame_shares(history):
    root = history["root"]
    found = bm.culprit_by_blame(str(root), history["f2"])
    assert found == (history["c2"], 1.0, 2)
    assert bm.culprit_by_blame(str(root), history["base"]) is None  # root commit: nothing removed


# --- a stub OpenRouter that answers reviews and judgements ------------------------------------------
class _Stub(BaseHTTPRequestHandler):
    judge_verdicts: dict[str, str] = {}
    requests: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.requests.append(body)
        names = {t["function"]["name"] for t in body.get("tools", [])}
        if "judge_detection" in names:
            text = body["messages"][1]["content"]
            verdict = "MISSED"
            for key, v in _Stub.judge_verdicts.items():
                if key in text:
                    verdict = v
            args = {"verdict": verdict, "matched_finding": 1 if verdict != "MISSED" else 0, "explanation": "stub"}
            calls = [
                {"id": "j", "type": "function", "function": {"name": "judge_detection", "arguments": json.dumps(args)}}
            ]
        else:
            opening = body["messages"][1]["content"]
            if "add transfer" in opening:
                findings = [
                    {
                        "file": "drivers/i2c/i2c_foo.c",
                        "line": 5,
                        "severity": "high",
                        "category": "concurrency",
                        "title": "Lock not released on error",
                        "problem": "Early return before unlock.",
                        "trigger": "bad() fails",
                        "impact": "deadlock",
                        "recommendation": "unlock",
                        "confidence": "high",
                    }
                ]
            else:
                findings = []
            review = {
                "summary": "s",
                "verdict": "request_changes" if findings else "approve",
                "confidence": "high",
                "findings": findings,
                "test_assessment": "",
                "commit_message_assessment": "",
            }
            calls = [
                {"id": "r", "type": "function", "function": {"name": "submit_review", "arguments": json.dumps(review)}}
            ]
        resp = {
            "id": "x",
            "model": "stub/model",
            "choices": [
                {"message": {"role": "assistant", "content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cost": 0.002},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub():
    _Stub.requests = []
    _Stub.judge_verdicts = {"release the lock": "DETECTED", "lost wakeup": "MISSED"}
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_judge_parses_tool_call(stub):
    llm = OpenRouter("k", ["stub/model"], base_url=stub)
    from zephyr_reviewer.review.schema import parse_review

    review, _ = parse_review(
        {
            "summary": "s",
            "verdict": "comment",
            "confidence": "high",
            "findings": [
                {
                    "file": "a.c",
                    "line": 0,
                    "severity": "low",
                    "category": "bug",
                    "title": "t",
                    "problem": "p",
                    "trigger": "",
                    "impact": "",
                    "recommendation": "",
                    "confidence": "low",
                }
            ],
        },
        {},
    )
    j = judge(llm, "release the lock problem", review, "subj")
    assert j.verdict == "DETECTED" and j.matched_finding == 1 and j.model == "stub/model"
    assert "1. [low] t (a.c)" in findings_digest(review)
    assert findings_digest(None).startswith("(the review did not complete")
    j = judge(llm, "lost wakeup", review)
    assert j.verdict == "MISSED" and j.matched_finding == 0


def test_benchmark_mine_and_run_commands(history, stub, tmp_path, monkeypatch, capsys):
    root = history["root"]
    monkeypatch.setenv("ZR_BASE_URL", stub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    dataset = tmp_path / "bench.json"
    assert cli.main(["benchmark", "mine", "--repo", str(root), "--out", str(dataset)]) == 0
    entries = bm.load(str(dataset))
    assert len(entries) == 2

    runs = tmp_path / "runs"
    rc = cli.main(
        [
            "benchmark",
            "run",
            str(dataset),
            "--repo",
            str(root),
            "--output-dir",
            str(runs),
            "--concurrency",
            "2",
            "--model",
            "stub/model",
        ]
    )
    assert rc == 0
    run_dir = next(runs.iterdir())
    report = json.loads((run_dir / "results.json").read_text())
    s = report["summary"]
    assert s["entries"] == 2 and s["reviews_completed"] == 2 and s["judged"] == 2
    assert s["detection"] == {
        "DETECTED": 1,
        "PARTIALLY_DETECTED": 0,
        "MISSED": 1,
        "strict_rate": 0.5,
        "lenient_rate": 0.5,
    }
    assert s["noise"]["findings_total"] == 1 and s["noise"]["unmatched_findings_total"] == 0
    assert s["by_subsystem"] == {
        "Drivers: I2C": {"DETECTED": 1, "PARTIALLY_DETECTED": 0, "MISSED": 0},
        "Kernel": {"DETECTED": 0, "PARTIALLY_DETECTED": 0, "MISSED": 1},
    }
    assert s["by_source"] == {"blame": {"entries": 1, "detected": 0}, "trailer": {"entries": 1, "detected": 1}}
    assert s["resources"]["steps"]["mean"] == 1.0 and s["resources"]["total_cost_usd"] == 0.004
    assert report["metadata"]["judge_model"] == "stub/model" and report["metadata"]["models"]
    by_commit = {r["commit"]: r for r in report["results"]}
    assert by_commit[history["c1"]]["matched_title"] == "Lock not released on error"
    assert by_commit[history["c2"]]["verdict"] == "MISSED" and by_commit[history["c2"]]["findings"] == 0
    # Per-entry artifacts and the judgement are preserved.
    d1 = run_dir / history["c1"][:12]
    assert {p.name for p in d1.iterdir()} >= {
        "entry.json",
        "review.json",
        "comment.md",
        "transcript.jsonl",
        "judgement.json",
    }
    summary = (run_dir / "summary.md").read_text()
    assert "| 2 | 2 | 2 | 1 | 0 | 1 | 50.0% | 50.0% |" in summary and "## By subsystem" in summary
    assert summary in capsys.readouterr().out
    # The review calls ran against each culprit with its parent as base.
    review_calls = [r for r in _Stub.requests if "submit_review" in {t["function"]["name"] for t in r["tools"]}]
    assert len(review_calls) == 2 and all("### Full diff" in r["messages"][1]["content"] for r in review_calls)

    # Re-judging an existing run with a different judge outcome, without reviewing again.
    _Stub.requests = []
    _Stub.judge_verdicts = {"release the lock": "PARTIALLY_DETECTED", "lost wakeup": "MISSED"}
    rc = cli.main(["benchmark", "run", str(dataset), "--repo", str(root), "--analyze-only", str(run_dir)])
    assert rc == 0
    report2 = json.loads((run_dir / "results.json").read_text())
    assert (
        report2["summary"]["detection"]["PARTIALLY_DETECTED"] == 1
        and report2["summary"]["detection"]["strict_rate"] == 0.0
    )
    assert all(
        "judge_detection" in {t["function"]["name"] for t in r["tools"]} for r in _Stub.requests
    )  # no reviews re-run
    assert report2["summary"]["resources"]["tokens"]["mean"] == 110.0  # resources recovered from saved artifacts


def test_render_summary_handles_empty_report():
    report = {
        "metadata": {
            "run_dir": "x",
            "created": "now",
            "zephyr_reviewer": "0",
            "repo_head": "abc",
            "models": ["m"],
            "draft_models": [],
            "judge_model": None,
            "profile": "zephyr",
            "max_steps": 1,
        },
        "summary": {
            "entries": 0,
            "reviews_completed": 0,
            "reviews_failed": 0,
            "judged": 0,
            "detection": {
                "DETECTED": 0,
                "PARTIALLY_DETECTED": 0,
                "MISSED": 0,
                "strict_rate": None,
                "lenient_rate": None,
            },
            "noise": {
                "findings_total": 0,
                "findings_per_review": {"mean": 0, "p50": 0, "p90": 0, "max": 0},
                "unmatched_findings_total": 0,
                "unmatched_per_review": {"mean": 0, "p50": 0, "p90": 0, "max": 0},
                "unmatched_by_severity": {},
                "verdicts": {},
            },
            "resources": {
                k: {"mean": 0, "p50": 0, "p90": 0, "max": 0}
                for k in ("steps", "tool_calls", "tokens", "cost_usd", "seconds")
            }
            | {"total_cost_usd": 0, "judge_tokens": 0, "judge_cost_usd": 0},
            "by_subsystem": {},
            "by_source": {},
        },
        "results": [],
    }
    text = render_summary(report)
    assert "| 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a |" in text
