# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Configuration from CLI flags and GitHub Actions ``INPUT_*`` environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

SEVERITY_RANK = {"none": 0, "suggestion": 1, "low": 2, "medium": 3, "high": 4, "critical": 5}


def _env(name: str, default: str = "") -> str:
    v = os.environ.get("INPUT_" + name.upper())
    if v is None:
        v = os.environ.get("ZR_" + name.upper())
    return default if v is None else v


def _bool(v: str | bool | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(v: str) -> list[str]:
    return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]


@dataclass
class ReviewConfig:
    repo_root: str = "."
    repo_slug: str = ""
    pr_number: int | None = None
    head: str = "HEAD"
    base: str = ""
    fetch_missing: bool = True

    openrouter_api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    models: list[str] = field(default_factory=lambda: ["anthropic/claude-sonnet-4.5"])
    provider: dict | None = None
    draft_models: list[str] = field(default_factory=list)  # non-empty enables the two-stage review
    verify_max_steps: int = 15
    dossier_chars: int = 40_000
    temperature: float = 0.1
    max_tokens: int = 8000
    max_steps: int = 40
    max_context_tokens: int = 160_000
    max_tool_result_chars: int = 24_000
    inline_diff_chars: int = 60_000
    generate_test_plan: bool = False
    include_discussion: bool = True
    max_discussion_chars: int = 12_000
    language: str = "English"

    profile: str = "zephyr"
    project_context: str = ""

    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    post_comment: bool = True
    post_inline: bool = False
    comment_tag: str = "zephyr-reviewer"
    fail_on_severity: str = "none"
    dry_run: bool = False
    in_process_server: bool = False
    output_dir: str = "review-output"

    skip_labels: list[str] = field(default_factory=lambda: ["no-ai-review", "skip-review"])
    required_labels: list[str] = field(default_factory=list)
    skip_draft: bool = True

    @classmethod
    def from_env(cls) -> ReviewConfig:
        cfg = cls()
        cfg.repo_root = _env("repo_root", os.environ.get("GITHUB_WORKSPACE", "."))
        cfg.repo_slug = _env("repository", os.environ.get("GITHUB_REPOSITORY", ""))
        pr = _env("pr_number")
        cfg.pr_number = int(pr) if pr.strip().isdigit() else None
        cfg.head = _env("head", "HEAD")
        cfg.base = _env("base", "")
        cfg.openrouter_api_key = _env("openrouter_api_key", os.environ.get("OPENROUTER_API_KEY", ""))
        cfg.base_url = _env("base_url", cfg.base_url)
        models = [_env("model", cfg.models[0])] + _list(_env("fallback_models"))
        cfg.models = [m for i, m in enumerate(models) if m and m not in models[:i]]
        prov = _env("provider")
        cfg.provider = json.loads(prov) if prov.strip() else None
        drafts = [_env("draft_model")] + _list(_env("draft_fallback_models"))
        cfg.draft_models = [m for i, m in enumerate(drafts) if m and m not in drafts[:i]]
        cfg.verify_max_steps = int(_env("verify_max_steps", str(cfg.verify_max_steps)))
        cfg.dossier_chars = int(_env("dossier_chars", str(cfg.dossier_chars)))
        cfg.temperature = float(_env("temperature", str(cfg.temperature)))
        cfg.max_tokens = int(_env("max_tokens", str(cfg.max_tokens)))
        cfg.max_steps = int(_env("max_steps", str(cfg.max_steps)))
        cfg.max_context_tokens = int(_env("max_context_tokens", str(cfg.max_context_tokens)))
        cfg.max_tool_result_chars = int(_env("max_tool_result_chars", str(cfg.max_tool_result_chars)))
        cfg.inline_diff_chars = int(_env("inline_diff_chars", str(cfg.inline_diff_chars)))
        cfg.generate_test_plan = _bool(_env("generate_test_plan"), False)
        cfg.include_discussion = _bool(_env("include_discussion"), True)
        cfg.max_discussion_chars = int(_env("max_discussion_chars", str(cfg.max_discussion_chars)))
        cfg.language = _env("language", cfg.language)
        cfg.profile = _env("review_profile", cfg.profile)
        cfg.project_context = _env("project_context")
        cfg.github_token = _env("github_token", os.environ.get("GITHUB_TOKEN", ""))
        cfg.github_api_url = os.environ.get("GITHUB_API_URL", cfg.github_api_url)
        cfg.post_comment = _bool(_env("post_comment"), True)
        cfg.post_inline = _bool(_env("post_inline_comments"), False)
        cfg.comment_tag = _env("comment_tag", cfg.comment_tag)
        cfg.fail_on_severity = _env("fail_on_severity", "none").strip().lower() or "none"
        cfg.dry_run = _bool(_env("dry_run"), False)
        cfg.output_dir = _env("output_dir", cfg.output_dir)
        cfg.skip_labels = _list(_env("skip_labels", ",".join(cfg.skip_labels)))
        cfg.required_labels = _list(_env("required_labels"))
        cfg.skip_draft = _bool(_env("skip_draft"), True)
        return cfg

    def validate(self) -> list[str]:
        errors = []
        if not self.openrouter_api_key:
            errors.append("OpenRouter API key missing (--api-key, OPENROUTER_API_KEY or INPUT_OPENROUTER_API_KEY)")
        if self.fail_on_severity not in SEVERITY_RANK:
            errors.append(f"fail_on_severity must be one of {', '.join(SEVERITY_RANK)}")
        if not self.models:
            errors.append("at least one model is required")
        if self.pr_number is None and not self.base:
            errors.append("either a pull request number or --base is required")
        if self.pr_number is not None and not self.repo_slug:
            errors.append("--repository owner/name is required with a pull request number")
        if self.pr_number is not None and not self.github_token and not self.dry_run:
            errors.append("GitHub token missing (--github-token, GITHUB_TOKEN or INPUT_GITHUB_TOKEN)")
        return errors
