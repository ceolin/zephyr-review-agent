# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Which twister scenarios exercise a path or symbol."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from .maintainers import _match_fn
from .repo import GitRepo

_META_FILES = ("tests.yaml", "testcase.yaml", "sample.yaml")


@dataclass
class TestSuite:
    meta_path: str
    scenarios: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)  # integration_platforms / platform_allow
    integration_platforms: list[str] = field(default_factory=list)
    platform_allow: list[str] = field(default_factory=list)
    platform_exclude: list[str] = field(default_factory=list)
    arch_allow: list[str] = field(default_factory=list)
    harness: list[str] = field(default_factory=list)
    build_only: bool = False
    why: str = ""

    @property
    def directory(self) -> str:
        return _suite_dir(self.meta_path)

    def describe(self, max_scenarios: int = 12) -> list[str]:
        out = [f"## {self.directory}  ({self.meta_path}; {self.why})"]
        if self.scenarios:
            more = f" (+{len(self.scenarios) - max_scenarios} more)" if len(self.scenarios) > max_scenarios else ""
            out.append("  scenarios: " + ", ".join(self.scenarios[:max_scenarios]) + more)
        if self.tags:
            out.append("  tags: " + ", ".join(self.tags))
        if self.integration_platforms:
            out.append("  integration_platforms: " + ", ".join(self.integration_platforms[:12]))
        if self.platform_allow:
            out.append("  platform_allow: " + ", ".join(self.platform_allow[:16]))
        if self.arch_allow:
            out.append("  arch_allow: " + ", ".join(self.arch_allow))
        if self.platform_exclude:
            out.append("  platform_exclude: " + ", ".join(self.platform_exclude[:10]))
        if self.harness:
            out.append("  harness: " + ", ".join(self.harness))
        if self.build_only:
            out.append("  build_only: yes (some or all scenarios)")
        return out


def _suite_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _load_meta(repo: GitRepo, meta_path: str, why: str) -> TestSuite | None:
    text = repo.show(meta_path, max_bytes=200_000)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return TestSuite(meta_path, why=why + " (unparseable yaml)")
    suite = TestSuite(meta_path, why=why)
    common = data.get("common") or {}
    tests = data.get("tests") or {}
    if isinstance(tests, dict):
        suite.scenarios = sorted(str(k) for k in tests)
    collected: dict[str, set[str]] = {
        k: set()
        for k in ("tags", "integration_platforms", "platform_allow", "platform_exclude", "arch_allow", "harness")
    }
    for block in [common, *(tests.values() if isinstance(tests, dict) else [])]:
        if not isinstance(block, dict):
            continue
        for key in collected:
            v = block.get(key)
            if isinstance(v, str):
                collected[key].update(v.split())
            elif isinstance(v, list):
                collected[key].update(map(str, v))
        if block.get("build_only"):
            suite.build_only = True
    suite.tags = sorted(collected["tags"])
    suite.integration_platforms = sorted(collected["integration_platforms"])
    suite.platform_allow = sorted(collected["platform_allow"])
    suite.platform_exclude = sorted(collected["platform_exclude"])
    suite.arch_allow = sorted(collected["arch_allow"])
    suite.harness = sorted(collected["harness"])
    suite.platforms = sorted(collected["integration_platforms"] | collected["platform_allow"])
    return suite


def suite_containing(repo: GitRepo, path: str) -> TestSuite | None:
    """For a path inside tests/ or samples/, the metadata of the enclosing suite."""
    d = _suite_dir(path)
    files = set(repo.files())
    while True:
        for meta in _META_FILES:
            cand = f"{d}/{meta}" if d else meta
            if cand in files:
                return _load_meta(repo, cand, f"encloses {path}")
        if not d:
            return None
        d = _suite_dir(d)


def suites_under(repo: GitRepo, directory: str, limit: int = 30) -> list[TestSuite]:
    prefix = directory.rstrip("/") + "/"
    out: list[TestSuite] = []
    for p in repo.files():
        if p.startswith(prefix) and p.rsplit("/", 1)[-1] in _META_FILES:
            s = _load_meta(repo, p, f"under {directory}")
            if s:
                out.append(s)
            if len(out) >= limit:
                break
    return out


def suites_referencing(repo: GitRepo, symbol: str, limit: int = 30) -> list[TestSuite]:
    """Suites whose sources mention ``symbol`` (tests/ and samples/ only)."""
    hits, _ = repo.grep(symbol, paths="tests/**", regex=False, word=True, max_results=2000)
    hits2, _ = repo.grep(symbol, paths="samples/**", regex=False, word=True, max_results=500)
    seen: dict[str, int] = {}
    for h in [*hits, *hits2]:
        d = _suite_dir(h.path)
        # Walk up to the suite root (dir that holds a metadata file).
        files = set(repo.files())
        while d and not any(f"{d}/{m}" in files for m in _META_FILES):
            d = _suite_dir(d)
        if d:
            seen[d] = seen.get(d, 0) + 1
    out: list[TestSuite] = []
    for d, n in sorted(seen.items(), key=lambda kv: -kv[1])[:limit]:
        for m in _META_FILES:
            if repo.exists(f"{d}/{m}"):
                s = _load_meta(repo, f"{d}/{m}", f"{n} reference(s) to {symbol}")
                if s:
                    out.append(s)
                break
    return out


def changed_suite_dirs(repo: GitRepo, changed: list[str]) -> dict[str, list[str]]:
    """Suite directories owning changed files, as scripts/ci/test_plan.py resolves them.

    Walks up from each changed file to a directory holding a metadata file. A ``common`` directory
    without one maps to every suite beside it. Returns {suite_dir: [changed files]}.
    """
    files = set(repo.files())
    out: dict[str, list[str]] = {}
    for f in changed:
        if f.endswith(".rst"):
            continue
        d = _suite_dir(f)
        while d:
            if any(f"{d}/{m}" in files for m in _META_FILES):
                out.setdefault(d, []).append(f)
                break
            head, _, tail = d.rpartition("/")
            if tail == "common" and head:
                siblings = sorted(
                    {_suite_dir(p) for p in files if p.startswith(head + "/") and p.rsplit("/", 1)[-1] in _META_FILES}
                )
                for s_dir in siblings:
                    out.setdefault(s_dir, []).append(f)
                if siblings:
                    break
            d = head
    return out


def suite_for_dir(repo: GitRepo, directory: str, why: str) -> TestSuite | None:
    for m in _META_FILES:
        if repo.exists(f"{directory}/{m}"):
            return _load_meta(repo, f"{directory}/{m}", why)
    return None


def suites_with_tags(repo: GitRepo, tags: list[str], limit: int = 40) -> list[TestSuite]:
    """Suites whose metadata mentions any of ``tags`` as a word (then confirmed by parsing)."""
    if not tags:
        return []
    pattern = "|".join(re.escape(t) for t in tags)
    hits, _ = repo.grep(pattern, paths="tests/**", regex=True, word=True, max_results=4000)
    metas = sorted({h.path for h in hits if h.path.rsplit("/", 1)[-1] in _META_FILES})
    out: list[TestSuite] = []
    wanted = set(tags)
    for meta in metas:
        s = _load_meta(repo, meta, "")
        if s and wanted & set(s.tags):
            s.why = "tagged " + ", ".join(sorted(wanted & set(s.tags)))
            out.append(s)
        if len(out) >= limit:
            break
    return out


class Tags:
    """scripts/ci/tags.yaml: which twister tags a path implicates."""

    def __init__(self, text: str):
        data = yaml.safe_load(text) or {}
        self._tags: list[tuple[str, object, object]] = []
        for name, d in data.items():
            if isinstance(d, dict):
                self._tags.append(
                    (
                        str(name),
                        _match_fn(d.get("files"), d.get("files-regex")),
                        _match_fn(d.get("files-exclude"), d.get("files-regex-exclude")),
                    )
                )

    @classmethod
    def from_repo(cls, repo: GitRepo, path: str = "scripts/ci/tags.yaml") -> Tags | None:
        text = repo.show(path)
        return cls(text) if text else None

    def for_paths(self, paths: list[str]) -> list[str]:
        out: set[str] = set()
        for name, match, exclude in self._tags:
            for p in paths:
                if match and match(p) and not (exclude and exclude(p)):
                    out.add(name)
                    break
        return sorted(out)
