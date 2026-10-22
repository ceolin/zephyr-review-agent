# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Mine a benchmark dataset from fix commits.

Each entry pairs a commit that introduced a bug with the commit that fixed it and a description of
the bug (the fix's commit message). Two sources:

- ``trailer``: the fix carries ``Fixes: <sha> ("subject")``; exact.
- ``blame``: the fix references an issue or nothing; the lines it removes are blamed at the fix's
  parent and the commit that introduced most of them is taken as the culprit, with that share
  recorded as ``confidence``. This is how people derive Fixes tags by hand.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field

from ..intel.maintainers import Maintainers
from ..intel.repo import GitRepo

_FIXES_SHA = re.compile(r"^Fixes:\s*([0-9a-f]{7,40})\b", re.M)
_TRAILER = re.compile(
    r"^(Signed-off-by|Fixes|Reviewed-by|Acked-by|Tested-by|Co-authored-by|Assisted-by|Link|Reported-by|"
    r"Suggested-by|Cc|Closes|Resolves|Change-Id|Fixes parts of):?\s",
    re.I,
)
_ISSUE_LINE = re.compile(r"^(Fixes|Closes|Resolves)\s*:?\s*(zephyrproject-rtos/zephyr)?#\d+", re.I)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_BLAME_HEAD = re.compile(r"^([0-9a-f]{40}) \d+ \d+(?: \d+)?$")
_SOURCE_EXT = (".c", ".h", ".S", ".dts", ".dtsi", ".overlay", ".yaml", ".yml", ".cmake", ".py", ".conf", ".ld")


@dataclass
class Entry:
    commit: str
    fixed_by: str
    source: str  # trailer | blame
    confidence: float
    subsystem: str
    files: list[str]
    problem_description: str
    commit_subject: str
    fix_subject: str
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=False).stdout


def _is_source(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return path.endswith(_SOURCE_EXT) or base.startswith("Kconfig") or base == "CMakeLists.txt"


def describe_problem(subject: str, body: str) -> str:
    """The fix's message without trailers and issue references: what was wrong and why."""
    lines = []
    for line in body.splitlines():
        if _TRAILER.match(line) or _ISSUE_LINE.match(line):
            continue
        lines.append(line)
    text = " ".join(" ".join(lines).split())
    return f"{subject}. {text}".strip() if text else subject


def culprit_by_blame(root: str, fix_sha: str, max_files: int = 20) -> tuple[str, float, int] | None:
    """(sha, share, removed_lines) of the commit that introduced most of the lines the fix removes."""
    diff = _git(root, "diff", "-U0", "--no-color", f"{fix_sha}^", fix_sha)
    path = None
    ranges: dict[str, list[tuple[int, int]]] = {}
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            path = line[6:]
        elif line.startswith("--- /dev/null"):
            path = None
        elif line.startswith("@@") and path and _is_source(path):
            m = _HUNK.match(line)
            if m:
                start, length = int(m.group(1)), int(m.group(2) or 1)
                if length > 0:
                    ranges.setdefault(path, []).append((start, start + length - 1))
    if not ranges or len(ranges) > max_files:
        return None
    counts: Counter[str] = Counter()
    for p, rs in ranges.items():
        args = ["blame", "--porcelain", f"{fix_sha}^", "--"]
        for s, e in rs[:40]:
            args[-1:-1] = ["-L", f"{s},{e}"]
        out = _git(root, *args, p)
        for ln in out.splitlines():
            m = _BLAME_HEAD.match(ln)
            if m:
                counts[m.group(1)] += 1
    total = sum(counts.values())
    if not total:
        return None
    sha, n = counts.most_common(1)[0]
    return sha, n / total, total


def mine(
    root: str,
    rev_range: str = "HEAD",
    *,
    since: str | None = None,
    max_entries: int = 200,
    use_blame: bool = True,
    min_confidence: float = 0.6,
    min_lines: int = 2,
    max_commit_files: int = 30,
    log=lambda m: None,
) -> list[Entry]:
    args = ["log", "--no-merges", "--format=%H%x00%s%x00%b%x1e", rev_range]
    if since:
        args.insert(2, f"--since={since}")
    raw = _git(root, *args)
    entries: list[Entry] = []
    seen: set[str] = set()
    maintainers_cache: dict[str, Maintainers | None] = {}
    for rec in raw.split("\x1e"):
        if len(entries) >= max_entries:
            break
        parts = rec.strip("\n").split("\x00")
        if len(parts) < 3:
            continue
        fix_sha, subject, body = parts[0].strip(), parts[1], parts[2]
        if not fix_sha:
            continue
        culprit: str | None = None
        source, confidence, extra = "", 0.0, {}
        m = _FIXES_SHA.search(body)
        if m:
            full = _git(root, "rev-parse", "--verify", "--quiet", f"{m.group(1)}^{{commit}}").strip()
            if full:
                culprit, source, confidence = full, "trailer", 1.0
        if culprit is None and use_blame and (_ISSUE_LINE.search(body) or re.search(r"\bfix(es|ed)?\b", subject, re.I)):
            found = culprit_by_blame(root, fix_sha)
            if found and found[1] >= min_confidence and found[2] >= min_lines:
                culprit, source, confidence = found[0], "blame", round(found[1], 3)
                extra = {"blamed_lines": found[2]}
        if culprit is None or culprit in seen or culprit == fix_sha:
            continue
        # The culprit must be a reviewable, non-merge commit touching source files.
        parents = _git(root, "rev-list", "--parents", "-n", "1", culprit).split()
        if len(parents) != 2:  # root commit or merge
            continue
        files = [f for f in _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", culprit).splitlines() if f]
        if not files or len(files) > max_commit_files or not any(_is_source(f) for f in files):
            continue
        culprit_subject = _git(root, "log", "-1", "--format=%s", culprit).strip()
        if fix_sha not in maintainers_cache:
            try:
                maintainers_cache[fix_sha] = Maintainers.from_repo(GitRepo(root, fix_sha))
            except Exception:  # noqa: BLE001 - optional metadata
                maintainers_cache[fix_sha] = None
        subsystem = _subsystem(maintainers_cache[fix_sha], files)
        seen.add(culprit)
        entries.append(
            Entry(
                commit=culprit,
                fixed_by=fix_sha,
                source=source,
                confidence=confidence,
                subsystem=subsystem,
                files=files,
                problem_description=describe_problem(subject, body),
                commit_subject=culprit_subject,
                fix_subject=subject,
                extra=extra,
            )
        )
        log(f"{len(entries):4d} {source:7s} {confidence:.2f} {culprit[:12]} <- {fix_sha[:12]} {subject[:60]}")
    return entries


def _subsystem(m: Maintainers | None, files: list[str]) -> str:
    if m is None:
        return ""
    counts: Counter[str] = Counter()
    for f in files:
        for area, _ in m.areas_for(f):
            counts[area.name] += 1
    return counts.most_common(1)[0][0] if counts else ""


def save(entries: list[Entry], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([e.to_dict() for e in entries], fh, indent=2)
        fh.write("\n")


def load(path: str) -> list[Entry]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for d in data:
        d = dict(d)
        known = {k: d.pop(k) for k in list(d) if k in Entry.__dataclass_fields__}
        known.setdefault("extra", {})
        known.setdefault("files", [])
        known.setdefault("source", "manual")
        known.setdefault("confidence", 1.0)
        known.setdefault("subsystem", "")
        known.setdefault("commit_subject", "")
        known.setdefault("fix_subject", "")
        known.setdefault("fixed_by", "")
        out.append(Entry(**known))
    return out
