# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Which twister scenarios exercise a path or symbol."""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from .repo import GitRepo

_META_FILES = ("tests.yaml", "testcase.yaml", "sample.yaml")


@dataclass
class TestSuite:
    meta_path: str
    scenarios: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)  # integration_platforms / platform_allow
    why: str = ""


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
    tags: set[str] = set()
    plats: set[str] = set()
    for block in [common, *(tests.values() if isinstance(tests, dict) else [])]:
        if not isinstance(block, dict):
            continue
        t = block.get("tags")
        if isinstance(t, str):
            tags.update(t.split())
        elif isinstance(t, list):
            tags.update(map(str, t))
        for key in ("integration_platforms", "platform_allow"):
            p = block.get(key)
            if isinstance(p, str):
                plats.update(p.split())
            elif isinstance(p, list):
                plats.update(map(str, p))
    suite.tags = sorted(tags)
    suite.platforms = sorted(plats)
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
