# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Run the reviewer over a benchmark dataset, judge each result, and report."""

from __future__ import annotations

import asyncio
import json
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..intel import SourceIntel
from ..review import publish as pub
from ..review.config import ReviewConfig
from ..review.github_api import PullRequest
from ..review.openrouter import OpenRouter
from ..review.render import inline_comments, render_comment, render_failure
from ..review.schema import SEVERITIES, Review
from .judge import Judgement, judge
from .mine import Entry


@dataclass
class EntryResult:
    entry: Entry
    directory: str
    review: Review | None
    errors: list[str]
    judgement: Judgement | None
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    models: list[str] = field(default_factory=list)

    @property
    def unmatched(self) -> list:
        if self.review is None:
            return []
        matched = self.judgement.matched_finding if self.judgement else 0
        return [f for i, f in enumerate(self.review.findings, 1) if i != matched]


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False).stdout.strip()


def _config_for(cfg: ReviewConfig, root: str, entry: Entry) -> ReviewConfig:
    c = ReviewConfig(**{k: getattr(cfg, k) for k in cfg.__dataclass_fields__})
    c.repo_root = root
    c.repo_slug = ""
    c.pr_number = None
    c.head = entry.commit
    c.base = f"{entry.commit}^"
    c.publish = "never"
    c.include_discussion = False
    c.include_ci = False
    c.in_process_server = True
    c.generate_test_plan = False
    c.fetch_missing = False
    return c


async def review_entry(cfg: ReviewConfig, root: str, entry: Entry, out_dir: Path, log) -> EntryResult:
    from ..cli import _run  # local import: cli imports this package

    entry_cfg = _config_for(cfg, root, entry)
    d = out_dir / entry.commit[:12]
    d.mkdir(parents=True, exist_ok=True)
    (d / "entry.json").write_text(json.dumps(entry.to_dict(), indent=2), encoding="utf-8")
    subject = _git(root, "log", "-1", "--format=%s", entry.commit)
    body = _git(root, "log", "-1", "--format=%b", entry.commit)
    pr = PullRequest(
        0,
        subject,
        body,
        "",
        f"{entry.commit[:12]}^",
        _git(root, "rev-parse", f"{entry.commit}^"),
        entry.commit[:12],
        entry.commit,
        "",
        False,
    )
    started = time.monotonic()
    try:
        intel = SourceIntel(root, entry_cfg.head, entry_cfg.base, max_chars=entry_cfg.max_tool_result_chars)
        run = await _run(entry_cfg, pr, intel)
    except Exception as exc:  # noqa: BLE001 - one broken entry must not stop the benchmark
        log(f"{entry.commit[:12]}: review failed: {exc}")
        (d / "error.txt").write_text(str(exc), encoding="utf-8")
        return EntryResult(entry, str(d), None, [str(exc)], None, seconds=time.monotonic() - started)
    if run.review is not None:
        body_md = render_comment(run.review, run, pr, repo="", tag="benchmark", details=True)
        inline = inline_comments(run.review, "benchmark")
    else:
        body_md, inline = render_failure(run, pr, "benchmark"), []
    pub.save_artifacts(str(d), run, body_md, pr, entry_cfg, inline)
    return EntryResult(
        entry,
        str(d),
        run.review,
        list(run.errors),
        None,
        steps=run.steps,
        tool_calls=run.tool_call_count,
        tokens=run.usage.total_tokens,
        cost=run.usage.cost,
        seconds=run.seconds,
        models=list(run.models_used),
    )


def load_entry_result(d: Path) -> EntryResult | None:
    """Rebuild an EntryResult from a saved entry directory (for --analyze-only)."""
    try:
        entry = Entry(
            **{k: v for k, v in json.loads((d / "entry.json").read_text()).items() if k in Entry.__dataclass_fields__}
        )
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    rec_path = d / "review.json"
    if not rec_path.exists():
        err = (d / "error.txt").read_text(encoding="utf-8") if (d / "error.txt").exists() else "no review.json"
        return EntryResult(entry, str(d), None, [err], None)
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    review = Review.from_dict(rec["review"]) if rec.get("review") else None
    usage = rec.get("usage") or {}
    return (
        EntryResult(
            entry,
            str(d),
            review,
            list(rec.get("errors") or []),
            None,
            steps=int(rec.get("steps") or 0),
            tool_calls=sum(1 for _ in []),  # not stored in review.json; recovered from transcript below
            tokens=int(usage.get("total_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            seconds=float(rec.get("seconds") or 0.0),
            models=list(rec.get("models") or []),
        )
        if True
        else None
    )


def _tool_calls_from_transcript(d: Path) -> int:
    p = d / "transcript.jsonl"
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("kind") == "tool_call" and t.get("name") != "submit_review":
            n += 1
    return n


async def run_benchmark(
    cfg: ReviewConfig,
    root: str,
    entries: list[Entry],
    out_dir: Path,
    *,
    judge_llm: OpenRouter | None,
    concurrency: int = 2,
    analyze_only: bool = False,
    log=print,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[EntryResult] = []
    if analyze_only:
        for e in entries:
            r = load_entry_result(out_dir / e.commit[:12])
            if r is None:
                r = EntryResult(e, str(out_dir / e.commit[:12]), None, ["not in run directory"], None)
            else:
                r.tool_calls = _tool_calls_from_transcript(Path(r.directory))
            results.append(r)
    else:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(e: Entry) -> EntryResult:
            async with sem:
                log(f"reviewing {e.commit[:12]} ({e.subsystem or 'unknown area'}): {e.commit_subject[:70]}")
                return await review_entry(cfg, root, e, out_dir, log)

        results = list(await asyncio.gather(*(one(e) for e in entries)))

    judge_usage = {"tokens": 0, "cost": 0.0}
    if judge_llm is not None:
        for r in results:
            if r.review is None and r.errors and not analyze_only and "review failed" in " ".join(r.errors):
                pass
            try:
                r.judgement = await asyncio.to_thread(
                    judge, judge_llm, r.entry.problem_description, r.review, r.entry.commit_subject
                )
            except Exception as exc:  # noqa: BLE001
                log(f"{r.entry.commit[:12]}: judge failed: {exc}")
                r.judgement = Judgement("MISSED", 0, f"judge failed: {exc}")
            if r.judgement.usage:
                judge_usage["tokens"] += int(r.judgement.usage.get("total_tokens") or 0)
                judge_usage["cost"] += float(r.judgement.usage.get("cost") or 0.0)
            (Path(r.directory) / "judgement.json").write_text(
                json.dumps(r.judgement.to_dict(), indent=2), encoding="utf-8"
            )
            log(
                f"{r.entry.commit[:12]}: {r.judgement.verdict}"
                + (f" (finding {r.judgement.matched_finding})" if r.judgement.matched_finding else "")
            )

    report = build_report(results, cfg, judge_llm, judge_usage, out_dir, root)
    (out_dir / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(render_summary(report), encoding="utf-8")
    return report


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[k]


def _dist(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "mean": round(statistics.fmean(values), 3),
        "p50": round(_pct(values, 50), 3),
        "p90": round(_pct(values, 90), 3),
        "max": round(max(values), 3),
    }


def build_report(
    results: list[EntryResult],
    cfg: ReviewConfig,
    judge_llm: OpenRouter | None,
    judge_usage: dict,
    out_dir: Path,
    root: str,
) -> dict[str, Any]:
    judged = [r for r in results if r.judgement is not None]
    counts = {
        v: sum(1 for r in judged if r.judgement.verdict == v) for v in ("DETECTED", "PARTIALLY_DETECTED", "MISSED")
    }
    n = len(judged)
    completed = [r for r in results if r.review is not None]
    unmatched = [f for r in judged for f in r.unmatched]
    by_subsystem: dict[str, dict[str, int]] = {}
    for r in judged:
        s = by_subsystem.setdefault(
            r.entry.subsystem or "(none)", {"DETECTED": 0, "PARTIALLY_DETECTED": 0, "MISSED": 0}
        )
        s[r.judgement.verdict] += 1
    summary = {
        "entries": len(results),
        "reviews_completed": len(completed),
        "reviews_failed": len(results) - len(completed),
        "judged": n,
        "detection": {
            **counts,
            "strict_rate": round(counts["DETECTED"] / n, 3) if n else None,
            "lenient_rate": round((counts["DETECTED"] + counts["PARTIALLY_DETECTED"]) / n, 3) if n else None,
        },
        "noise": {
            "findings_total": sum(len(r.review.findings) for r in completed),
            "findings_per_review": _dist([len(r.review.findings) for r in completed]),
            "unmatched_findings_total": len(unmatched),
            "unmatched_per_review": _dist([len(r.unmatched) for r in judged]),
            "unmatched_by_severity": {s: sum(1 for f in unmatched if f.severity == s) for s in SEVERITIES},
            "verdicts": {
                v: sum(1 for r in completed if r.review.verdict == v) for v in ("approve", "comment", "request_changes")
            },
        },
        "resources": {
            "steps": _dist([float(r.steps) for r in completed]),
            "tool_calls": _dist([float(r.tool_calls) for r in completed]),
            "tokens": _dist([float(r.tokens) for r in completed]),
            "cost_usd": _dist([r.cost for r in completed]),
            "seconds": _dist([r.seconds for r in completed]),
            "total_cost_usd": round(sum(r.cost for r in completed), 4),
            "judge_tokens": judge_usage["tokens"],
            "judge_cost_usd": round(judge_usage["cost"], 4),
        },
        "by_subsystem": by_subsystem,
        "by_source": {
            src: {
                "entries": sum(1 for r in judged if r.entry.source == src),
                "detected": sum(1 for r in judged if r.entry.source == src and r.judgement.verdict == "DETECTED"),
            }
            for src in sorted({r.entry.source for r in results})
        },
    }
    return {
        "metadata": {
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "zephyr_reviewer": __version__,
            "run_dir": str(out_dir),
            "repo": root,
            "repo_head": _git(root, "rev-parse", "--short=12", "HEAD"),
            "models": cfg.models,
            "draft_models": cfg.draft_models,
            "judge_model": judge_llm.models[0] if judge_llm else None,
            "profile": cfg.profile,
            "max_steps": cfg.max_steps,
        },
        "summary": summary,
        "results": [
            {
                "commit": r.entry.commit,
                "fixed_by": r.entry.fixed_by,
                "source": r.entry.source,
                "confidence": r.entry.confidence,
                "subsystem": r.entry.subsystem,
                "commit_subject": r.entry.commit_subject,
                "problem_description": r.entry.problem_description,
                "directory": r.directory,
                "completed": r.review is not None,
                "errors": r.errors,
                "verdict": r.judgement.verdict if r.judgement else None,
                "matched_finding": r.judgement.matched_finding if r.judgement else None,
                "matched_title": (
                    r.review.findings[r.judgement.matched_finding - 1].title
                    if r.judgement and r.judgement.matched_finding and r.review
                    else None
                ),
                "explanation": r.judgement.explanation if r.judgement else None,
                "review_verdict": r.review.verdict if r.review else None,
                "findings": len(r.review.findings) if r.review else 0,
                "steps": r.steps,
                "tool_calls": r.tool_calls,
                "tokens": r.tokens,
                "cost_usd": round(r.cost, 4),
                "seconds": round(r.seconds, 1),
            }
            for r in results
        ],
    }


def render_summary(report: dict[str, Any]) -> str:
    m, s = report["metadata"], report["summary"]
    det, noise, res = s["detection"], s["noise"], s["resources"]

    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    out = [
        "# Benchmark summary",
        "",
        f"Run `{m['run_dir']}` on {m['created']} · zephyr-reviewer {m['zephyr_reviewer']} · "
        f"repo head `{m['repo_head']}`",
        f"Models: {', '.join(m['models'])}"
        + (f" (draft: {', '.join(m['draft_models'])})" if m["draft_models"] else "")
        + f" · judge: {m['judge_model'] or 'none'} · profile: {m['profile']} · max_steps: {m['max_steps']}",
        "",
        "## Detection",
        "",
        "| Entries | Completed | Judged | Detected | Partial | Missed | Strict | Lenient |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        f"| {s['entries']} | {s['reviews_completed']} | {s['judged']} | {det['DETECTED']} | "
        f"{det['PARTIALLY_DETECTED']} | {det['MISSED']} | {pct(det['strict_rate'])} | {pct(det['lenient_rate'])} |",
        "",
        "## Noise",
        "",
        f"Findings: {noise['findings_total']} total, {noise['findings_per_review']['mean']} per review (p90 "
        f"{noise['findings_per_review']['p90']}). Unmatched: {noise['unmatched_findings_total']} total, "
        f"{noise['unmatched_per_review']['mean']} per review. By severity: "
        + ", ".join(f"{k} {v}" for k, v in noise["unmatched_by_severity"].items() if v)
        + ". Review verdicts: "
        + ", ".join(f"{k} {v}" for k, v in noise["verdicts"].items()),
        "",
        "## Resources per review",
        "",
        "| Metric | Mean | p50 | p90 | Max |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key, label in (
        ("steps", "Steps"),
        ("tool_calls", "Tool calls"),
        ("tokens", "Tokens"),
        ("cost_usd", "Cost (USD)"),
        ("seconds", "Seconds"),
    ):
        d = res[key]
        out.append(f"| {label} | {d['mean']} | {d['p50']} | {d['p90']} | {d['max']} |")
    out += [
        "",
        f"Total review cost ${res['total_cost_usd']}, judge ${res['judge_cost_usd']} ({res['judge_tokens']:,} tokens).",
        "",
    ]
    if s["by_subsystem"]:
        out += ["## By subsystem", "", "| Subsystem | Detected | Partial | Missed |", "| --- | --- | --- | --- |"]
        for name, c in sorted(s["by_subsystem"].items(), key=lambda kv: -sum(kv[1].values())):
            out.append(f"| {name} | {c['DETECTED']} | {c['PARTIALLY_DETECTED']} | {c['MISSED']} |")
        out.append("")
    out += [
        "## Entries",
        "",
        "| Commit | Subsystem | Source | Verdict | Matched finding | Findings | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in report["results"]:
        verdict = r["verdict"] or ("FAILED" if not r["completed"] else "unjudged")
        matched = (r["matched_title"] or "")[:60]
        out.append(
            f"| `{r['commit'][:12]}` {r['commit_subject'][:50]} | {r['subsystem'] or ''} | {r['source']} | {verdict} | "
            f"{matched} | {r['findings']} | ${r['cost_usd']} |"
        )
    return "\n".join(out) + "\n"
