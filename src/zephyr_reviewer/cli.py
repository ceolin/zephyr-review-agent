# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""zephyr-reviewer: serve the source-intelligence MCP server, or run a review against it."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys

from . import mcp_server
from .intel import SourceIntel
from .review import publish as pub
from .review import testplan
from .review.agent import LocalTool, run_review
from .review.ci import CI_FAILURE_LOGS_TOOL, CI_STATUS_TOOL, CIResults
from .review.config import ReviewConfig
from .review.discussion import PR_DISCUSSION_TOOL, Discussion, trust_level
from .review.github_api import GitHub, PullRequest, pr_number_from_event
from .review.mcp_client import ToolBridge, stdio_params
from .review.openrouter import OpenRouter
from .review.prompts import load_profile, opening_message, system_prompt
from .review.render import inline_comments, preview_inline, render_comment, render_failure, run_details
from .review.stages import run_two_stage


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
    p.add_argument("--draft-model", help="Enable the two-stage review: this low-cost model investigates and drafts")
    p.add_argument("--draft-fallback-models", help="Comma-separated fallbacks for the draft model")
    p.add_argument("--verify-max-steps", type=int, help="Step budget of the verifying model in two-stage mode")
    p.add_argument("--api-key", help="OpenRouter API key (default: $OPENROUTER_API_KEY)")
    p.add_argument("--github-token", help="GitHub token (default: $GITHUB_TOKEN)")
    p.add_argument("--profile", help="Review profile name or path to a .md file (default: zephyr)")
    p.add_argument("--project-context", help="Extra guidance for the model")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--max-context-tokens", type=int)
    p.add_argument("--test-plan", action="store_true", help="Also produce a test plan: suites, scenarios, platforms")
    p.add_argument(
        "--lockdown",
        choices=["off", "label", "strict"],
        help="Handling of PR text from users without push access (can only tighten the operator setting)",
    )
    p.add_argument("--no-ci", action="store_true", help="Do not fetch CI results")
    p.add_argument(
        "--show-details", action="store_true", help="Append models, steps, tokens, cost and time to the posted comment"
    )
    p.add_argument("--inline", action="store_true", help="Post anchored findings as inline review comments (default)")
    p.add_argument("--no-inline", action="store_true", help="Put every finding in the top-level comment instead")
    p.add_argument("--no-comment", action="store_true", help="Do not post or update the summary comment")
    p.add_argument("--fail-on-severity", choices=["none", "suggestion", "low", "medium", "high", "critical"])
    p.add_argument("--dry-run", action="store_true", help="Run the review but print instead of posting (publish=never)")
    p.add_argument("--ask", action="store_true", help="Show the review and ask before posting (publish=ask)")
    p.add_argument(
        "--publish", choices=["post", "ask", "never"], help="Posting mode; can only tighten the operator setting"
    )
    p.add_argument("--in-process", action="store_true", help="Run the MCP server in-process instead of over stdio")
    p.add_argument("--output-dir", help="Where to write comment.md, review.json, transcript.jsonl")


def _config(args: argparse.Namespace) -> ReviewConfig:
    cfg = ReviewConfig.from_env()
    floor = cfg.policy()
    for attr, key in (
        ("repo_root", "repo_root"),
        ("repository", "repo_slug"),
        ("head", "head"),
        ("base", "base"),
        ("api_key", "openrouter_api_key"),
        ("github_token", "github_token"),
        ("profile", "profile"),
        ("project_context", "project_context"),
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
    if args.draft_model:
        cfg.draft_models = [args.draft_model]
    if args.draft_fallback_models:
        cfg.draft_models = cfg.draft_models[:1] + [
            m.strip() for m in args.draft_fallback_models.split(",") if m.strip()
        ]
    if args.verify_max_steps is not None:
        cfg.verify_max_steps = args.verify_max_steps
    if args.test_plan:
        cfg.generate_test_plan = True
    if args.no_ci:
        cfg.include_ci = False
    if args.show_details:
        cfg.show_run_details = True
    if args.in_process:
        cfg.in_process_server = True
    # Safety settings: the environment (workflow inputs) is the operator floor; flags may only tighten it.
    policy, rejected = floor.tighten(
        lockdown=args.lockdown,
        max_steps=args.max_steps,
        max_context_tokens=args.max_context_tokens,
        post_inline=False if args.no_inline else (True if args.inline else None),
        post_comment=False if args.no_comment else None,
        publish="never" if args.dry_run else ("ask" if args.ask else args.publish),
    )
    for r in rejected:
        log(f"::warning::ignored: {r}")
    cfg.apply_policy(policy)
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


def _load_discussion(cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest) -> Discussion | None:
    if gh is None or not pr.number or not cfg.include_discussion:
        return None
    d = Discussion.fetch(gh, pr.number, f"<!-- {cfg.comment_tag} -->", lockdown=cfg.lockdown, pr_author=pr.author)
    withheld = sum(1 for c in [*d.conversation, *d.inline, *d.reviews] if c.withheld)
    log(
        f"discussion: {len(d.conversation)} comment(s), {len(d.inline)} inline, {len(d.reviews)} review(s)"
        f"{', previous AI review present' if d.previous_ai else ''}"
        f"{', thread resolution known' if d.resolution_known else ''}"
        f"{f', {withheld} withheld (strict lockdown)' if withheld else ''}"
    )
    return d


def _load_ci(cfg: ReviewConfig, gh: GitHub | None, pr: PullRequest) -> CIResults | None:
    if gh is None or not pr.number or not cfg.include_ci:
        return None
    ci = CIResults(gh, pr.head_sha, max_chars=cfg.max_tool_result_chars)
    log(ci.summary_line())
    return ci


async def _run(
    cfg: ReviewConfig,
    pr: PullRequest,
    intel: SourceIntel,
    discussion: Discussion | None = None,
    ci: CIResults | None = None,
    author_trust: str = "",
):
    profile = load_profile(cfg.profile)
    system = system_prompt(profile, cfg.language)
    overview = intel.overview()
    diff = intel.diff_text if len(intel.diff_text) <= cfg.inline_diff_chars else None
    digest = discussion.digest(cfg.max_discussion_chars) if discussion else None
    opening = opening_message(
        pr,
        overview,
        diff,
        cfg.project_context,
        cfg.repo_slug,
        digest,
        cfg.generate_test_plan,
        author_trust=author_trust,
        ci_summary=ci.summary_line() if ci else "",
    )
    extra_tools = [LocalTool(PR_DISCUSSION_TOOL, discussion.tool_handler)] if discussion else []
    if ci:
        extra_tools += [LocalTool(CI_STATUS_TOOL, ci.status_tool), LocalTool(CI_FAILURE_LOGS_TOOL, ci.logs_tool)]
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
        if cfg.draft_models:
            draft_llm = OpenRouter(
                cfg.openrouter_api_key,
                cfg.draft_models,
                base_url=cfg.base_url,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                provider=cfg.provider,
            )
            run = await run_two_stage(
                draft_llm,
                llm,
                bridge,
                system,
                opening,
                changed,
                max_steps=cfg.max_steps,
                verify_max_steps=cfg.verify_max_steps,
                max_context_tokens=cfg.max_context_tokens,
                dossier_chars=cfg.dossier_chars,
                extra_tools=extra_tools,
                log=log,
            )
        else:
            run = await run_review(
                llm,
                bridge,
                system,
                opening,
                changed,
                max_steps=cfg.max_steps,
                max_context_tokens=cfg.max_context_tokens,
                extra_tools=extra_tools,
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
    author_trust = trust_level(gh, pr.author, False) if gh is not None and cfg.lockdown != "off" and pr.author else ""
    run = asyncio.run(_run(cfg, pr, intel, _load_discussion(cfg, gh, pr), _load_ci(cfg, gh, pr), author_trust))
    if run.review is not None and run.review.test_plan:
        testplan.annotate(run.review, intel)
        flagged = sum(1 for e in run.review.test_plan if e.notes)
        log(f"test plan: {len(run.review.test_plan)} entries, {flagged} flagged by validation")
    if run.review is not None:
        review = run.review

        def render(exclude_inline: bool) -> str:
            return render_comment(
                review,
                run,
                pr,
                repo=cfg.repo_slug,
                tag=cfg.comment_tag,
                api_url=cfg.github_api_url,
                details=cfg.show_run_details,
                exclude_inline=exclude_inline,
            )

        inline = inline_comments(review, cfg.comment_tag) if cfg.post_inline else []
    else:
        failure_body = render_failure(run, pr, cfg.comment_tag)
        log("::error::" + "; ".join(run.errors))

        def render(exclude_inline: bool) -> str:
            return failure_body

        inline = []
    body = render(bool(inline))
    out_dir = pub.save_artifacts(cfg.output_dir, run, body, pr, cfg, inline)
    log(f"artifacts in {out_dir}" + (f" ({len(inline)} inline comment(s))" if inline else ""))
    confirm = _interactive_confirm(cfg.output_dir, pr) if cfg.publish == "ask" and _has_terminal() else None
    outputs = pub.publish(cfg, gh, pr, run, render, log=log, confirm=confirm)
    pub.write_outputs(outputs)
    pub.write_summary(body + "\n\n" + run_details(run, pr))  # the step summary always carries the run details
    log(run_details(run, pr))
    if outputs.get("posted") != "true" and confirm is None:
        print(body)
        if inline:
            print("\n" + preview_inline(inline))
    failure = pub.gate_fails(run, cfg)
    if failure:
        log(f"::error::review gate failed: {failure}")
        return 1
    return 0


def _has_terminal() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _interactive_confirm(output_dir: str, pr: PullRequest):
    """Show the summary comment and every inline comment; ask to post, skip, or edit either file first."""
    comment_path = os.path.join(output_dir, "comment.md")
    inline_path = os.path.join(output_dir, "inline_comments.json")

    def edit(path: str) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.call([*editor.split(), path])

    def confirm(body: str, inline: list[dict]) -> tuple[bool, str, list[dict]]:
        while True:
            print("\n" + body + "\n", file=sys.stderr)
            if inline:
                print(preview_inline(inline) + "\n", file=sys.stderr)
            what = "the summary comment" + (f" and {len(inline)} inline comment(s)" if inline else "")
            target = f"PR #{pr.number}" if pr.number else "the pull request"
            choices = "[y]es / [n]o / [e]dit comment.md" + (" / [i] edit inline_comments.json" if inline else "")
            try:
                answer = input(f"Post {what} to {target}? {choices}: ").strip().lower()
            except EOFError:
                return False, body, inline
            if answer in ("y", "yes"):
                return True, body, inline
            if answer in ("n", "no", ""):
                return False, body, inline
            if answer in ("e", "edit"):
                edit(comment_path)
                with open(comment_path, encoding="utf-8") as fh:
                    body = fh.read()
                continue
            if answer in ("i", "inline") and inline:
                edit(inline_path)
                try:
                    with open(inline_path, encoding="utf-8") as fh:
                        inline = json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"inline_comments.json could not be read ({exc}); keeping the previous list", file=sys.stderr)
                continue
            print("please answer y, n, e" + (" or i" if inline else ""), file=sys.stderr)

    return confirm


def cmd_post(args: argparse.Namespace) -> int:
    repo = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    record, _ = pub.load_saved(args.output_dir)
    repo = repo or record.get("repo") or ""
    token = args.github_token or os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        log("::error::a repository (--repository or review.json) and a GitHub token are required")
        return 2
    gh = GitHub(token, repo, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    pr = PullRequest(int(record.get("pr") or 0), "", "", "", "", "", "", record.get("head_sha") or "", "", False)
    confirm = None if args.yes else (_interactive_confirm(args.output_dir, pr) if _has_terminal() else None)
    if confirm is None and not args.yes:
        log("::error::no terminal to confirm on; pass --yes to post without asking")
        return 2
    out = pub.post_saved(args.output_dir, gh, inline=args.inline, tag=args.comment_tag, log=log, confirm=confirm)
    return 0 if out.get("posted") == "true" else 1


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

    o = sub.add_parser("post", help="Publish a review saved by an earlier run (after reading or editing comment.md)")
    o.add_argument("--output-dir", default="review-output", help="Directory holding review.json and comment.md")
    o.add_argument("--repository", help="GitHub owner/name (default: from review.json or $GITHUB_REPOSITORY)")
    o.add_argument("--github-token", help="GitHub token (default: $GITHUB_TOKEN)")
    o.add_argument("--inline", action="store_true", help="Also post anchored findings as inline review comments")
    o.add_argument("--comment-tag", default="zephyr-reviewer")
    o.add_argument("--yes", action="store_true", help="Post without asking")
    o.set_defaults(fn=cmd_post)

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
