# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""zephyr-reviewer: serve the source-intelligence MCP server, or run a review against it."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

from . import mcp_server
from .intel import SourceIntel
from .review import publish as pub
from .review.agent import run_review
from .review.config import ReviewConfig
from .review.github_api import GitHub, PullRequest, pr_number_from_event
from .review.mcp_client import ToolBridge, stdio_params
from .review.openrouter import OpenRouter
from .review.prompts import load_profile, opening_message, system_prompt
from .review.render import render_comment, render_failure


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------- review
def _add_review_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", dest="repo_root", help="Zephyr checkout (default: $GITHUB_WORKSPACE or cwd)")
    p.add_argument("--repository", help="GitHub owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--pr", type=int, help="Pull request number (default: from the Actions event)")
    p.add_argument("--head", help="Commit under review when not using --pr (default: HEAD)")
    p.add_argument("--base", help="Base commit or ref when not using --pr")
    p.add_argument("--model", help="Primary OpenRouter model slug")
    p.add_argument("--fallback-models", help="Comma-separated fallback slugs")
    p.add_argument("--provider", help="OpenRouter provider routing preferences, JSON")
    p.add_argument("--api-key", help="OpenRouter API key (default: $OPENROUTER_API_KEY)")
    p.add_argument("--github-token", help="GitHub token (default: $GITHUB_TOKEN)")
    p.add_argument("--profile", help="Review profile name or path to a .md file (default: zephyr)")
    p.add_argument("--project-context", help="Extra guidance for the model")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--max-context-tokens", type=int)
    p.add_argument("--inline", action="store_true", help="Also post findings as inline review comments")
    p.add_argument("--no-comment", action="store_true", help="Do not post or update the summary comment")
    p.add_argument("--fail-on-severity", choices=["none", "suggestion", "low", "medium", "high", "critical"])
    p.add_argument("--dry-run", action="store_true", help="Run the review but print instead of posting")
    p.add_argument("--in-process", action="store_true", help="Run the MCP server in-process instead of over stdio")
    p.add_argument("--output-dir", help="Where to write comment.md, review.json, transcript.jsonl")


def _config(args: argparse.Namespace) -> ReviewConfig:
    cfg = ReviewConfig.from_env()
    for attr, key in (
        ("repo_root", "repo_root"),
        ("repository", "repo_slug"),
        ("head", "head"),
        ("base", "base"),
        ("api_key", "openrouter_api_key"),
        ("github_token", "github_token"),
        ("profile", "profile"),
        ("project_context", "project_context"),
        ("max_steps", "max_steps"),
        ("max_context_tokens", "max_context_tokens"),
        ("fail_on_severity", "fail_on_severity"),
        ("output_dir", "output_dir"),
    ):
        v = getattr(args, attr, None)
        if v is not None:
            setattr(cfg, key, v)
    if args.pr is not None:
        cfg.pr_number = args.pr
    elif cfg.pr_number is None and not args.base:
        cfg.pr_number = pr_number_from_event()
    if args.model:
        cfg.models = [args.model] + [m for m in cfg.models[1:] if m != args.model]
    if args.fallback_models:
        cfg.models = [cfg.models[0]] + [m.strip() for m in args.fallback_models.split(",") if m.strip()]
    if args.provider:
        cfg.provider = json.loads(args.provider)
    if args.inline:
        cfg.post_inline = True
    if args.no_comment:
        cfg.post_comment = False
    if args.dry_run:
        cfg.dry_run = True
    if args.in_process:
        cfg.in_process_server = True
    return cfg


def _ensure_commit(repo_root: str, sha: str, hint_ref: str | None) -> None:
    """Make sure ``sha`` is present locally, fetching it by SHA (or via a ref) when it is not."""
    if (
        subprocess.run(["git", "-C", repo_root, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode
        == 0
    ):
        return
    log(f"fetching {sha[:12]} ...")
    attempts = [["git", "-C", repo_root, "fetch", "--no-tags", "origin", sha]]
    if hint_ref:
        attempts.append(["git", "-C", repo_root, "fetch", "--no-tags", "origin", hint_ref])
    for cmd in attempts:
        if (
            subprocess.run(cmd, capture_output=True).returncode == 0
            and subprocess.run(
                ["git", "-C", repo_root, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
            ).returncode
            == 0
        ):
            return
    raise SystemExit(f"commit {sha} is not available in {repo_root} and could not be fetched")


def _resolve_pr(cfg: ReviewConfig) -> tuple[PullRequest, GitHub | None]:
    if cfg.pr_number is None:
        intel_head = subprocess.run(
            ["git", "-C", cfg.repo_root, "rev-parse", cfg.head], capture_output=True, text=True
        ).stdout.strip()
        intel_base = subprocess.run(
            ["git", "-C", cfg.repo_root, "rev-parse", cfg.base], capture_output=True, text=True
        ).stdout.strip()
        subject = subprocess.run(
            ["git", "-C", cfg.repo_root, "log", "-1", "--format=%s", cfg.head], capture_output=True, text=True
        ).stdout.strip()
        pr = PullRequest(
            number=0,
            title=subject or cfg.head,
            body="",
            author="",
            base_ref=cfg.base,
            base_sha=intel_base,
            head_ref=cfg.head,
            head_sha=intel_head,
            head_repo="",
            draft=False,
        )
        return pr, None
    gh = GitHub(cfg.github_token, cfg.repo_slug, cfg.github_api_url)
    pr = gh.pull(cfg.pr_number)
    merge_base = gh.merge_base(pr.base_sha, pr.head_sha) or pr.base_sha
    if cfg.fetch_missing:
        _ensure_commit(cfg.repo_root, pr.head_sha, f"pull/{pr.number}/head")
        _ensure_commit(cfg.repo_root, merge_base, pr.base_ref)
    pr.base_sha = merge_base
    cfg.head, cfg.base = pr.head_sha, merge_base
    return pr, gh


def _skip_reason(cfg: ReviewConfig, pr: PullRequest) -> str | None:
    if pr.number == 0:
        return None
    if cfg.skip_draft and pr.draft:
        return "pull request is a draft"
    labels = {lab.lower() for lab in pr.labels}
    hit = labels & {lab.lower() for lab in cfg.skip_labels}
    if hit:
        return f"label {sorted(hit)[0]!r} present"
    if cfg.required_labels and not labels & {lab.lower() for lab in cfg.required_labels}:
        return f"none of the required labels {cfg.required_labels} present"
    return None


async def _run(cfg: ReviewConfig, pr: PullRequest, intel: SourceIntel) -> tuple:
    profile = load_profile(cfg.profile)
    system = system_prompt(profile, cfg.language)
    overview = intel.overview()
    diff = intel.diff_text if len(intel.diff_text) <= cfg.inline_diff_chars else None
    opening = opening_message(pr, overview, diff, cfg.project_context, cfg.repo_slug)
    changed = {f.path: f.new_side_lines for f in intel.files}
    llm = OpenRouter(
        cfg.openrouter_api_key,
        cfg.models,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        provider=cfg.provider,
    )
    if cfg.in_process_server:
        target = mcp_server.build_server(intel)
    else:
        target = stdio_params(cfg.repo_root, cfg.head, cfg.base, cfg.max_tool_result_chars)
    async with ToolBridge(target, max_result_chars=cfg.max_tool_result_chars) as bridge:
        log(f"MCP server up with {len(bridge.tools)} tool(s); models: {', '.join(cfg.models)}")
        run = await run_review(
            llm,
            bridge,
            system,
            opening,
            changed,
            max_steps=cfg.max_steps,
            max_context_tokens=cfg.max_context_tokens,
            log=log,
        )
    return run


def cmd_review(args: argparse.Namespace) -> int:
    cfg = _config(args)
    errors = cfg.validate()
    if errors:
        for e in errors:
            log(f"::error::{e}")
        return 2
    pr, gh = _resolve_pr(cfg)
    reason = _skip_reason(cfg, pr)
    if reason:
        log(f"skipping review: {reason}")
        pub.write_outputs({"skipped": "true", "verdict": "skipped", "findings_count": 0, "highest_severity": "none"})
        return 0
    intel = SourceIntel(cfg.repo_root, cfg.head, cfg.base, max_chars=cfg.max_tool_result_chars)
    if not intel.files:
        log("nothing to review: empty diff")
        pub.write_outputs({"skipped": "true", "verdict": "skipped", "findings_count": 0, "highest_severity": "none"})
        return 0
    log(f"reviewing {len(intel.files)} file(s), {len(intel.commits)} commit(s): {cfg.base[:12]}..{cfg.head[:12]}")
    run = asyncio.run(_run(cfg, pr, intel))
    if run.review is not None:
        body = render_comment(run.review, run, pr, repo=cfg.repo_slug, tag=cfg.comment_tag, api_url=cfg.github_api_url)
    else:
        body = render_failure(run, pr, cfg.comment_tag)
        log("::error::" + "; ".join(run.errors))
    out_dir = pub.save_artifacts(cfg.output_dir, run, body, pr, cfg)
    log(f"artifacts in {out_dir}")
    outputs = pub.publish(cfg, gh, pr, run, body, log=log)
    pub.write_outputs(outputs)
    pub.write_summary(body)
    if cfg.dry_run or gh is None:
        print(body)
    failure = pub.gate_fails(run, cfg)
    if failure:
        log(f"::error::review gate failed: {failure}")
        return 1
    return 0


# ----------------------------------------------------------------------------- tools
def cmd_tools(args: argparse.Namespace) -> int:
    intel = SourceIntel(args.repo, args.head, args.base)
    srv = mcp_server.build_server(intel)

    async def go():
        async with ToolBridge(srv) as bridge:
            for t in bridge.tools:
                fn = t["function"]
                params = ", ".join(fn["parameters"].get("properties", {}))
                print(f"{fn['name']}({params})\n    {fn['description'].splitlines()[0]}")

    asyncio.run(go())
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    intel = SourceIntel(args.repo, args.head, args.base)
    srv = mcp_server.build_server(intel)
    kwargs = {}
    for item in args.args:
        k, _, v = item.partition("=")
        if v.isdigit():
            kwargs[k] = int(v)
        elif v.lower() in ("true", "false"):
            kwargs[k] = v.lower() == "true"
        else:
            kwargs[k] = v

    async def go():
        async with ToolBridge(srv, max_result_chars=10**7) as bridge:
            print(await bridge.call(args.tool, kwargs))

    asyncio.run(go())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="zephyr-reviewer", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the source-intelligence MCP server")
    mcp_server.add_server_args(s)
    s.set_defaults(fn=mcp_server.serve)

    r = sub.add_parser("review", help="Review a pull request (or a local range) and publish the result")
    _add_review_args(r)
    r.set_defaults(fn=cmd_review)

    t = sub.add_parser("tools", help="List the MCP tools as the model sees them")
    t.add_argument("--repo", default=".")
    t.add_argument("--head", default="HEAD")
    t.add_argument("--base", required=True)
    t.set_defaults(fn=cmd_tools)

    c = sub.add_parser("call", help="Invoke one MCP tool locally, e.g. call find_definition name=k_sem_take")
    c.add_argument("--repo", default=".")
    c.add_argument("--head", default="HEAD")
    c.add_argument("--base", required=True)
    c.add_argument("tool")
    c.add_argument("args", nargs="*", help="key=value arguments")
    c.set_defaults(fn=cmd_call)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
