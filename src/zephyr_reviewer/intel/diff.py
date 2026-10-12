# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Unified diff parsing: changed files, hunks, and the new-side line numbers.

The new-side line set is what decides whether a finding can be attached as an
inline review comment: GitHub only accepts comments on lines that appear in
the diff, and a model that cites a line outside it is usually citing context
rather than a change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: list[str] = field(default_factory=list)

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_len, 1) - 1


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    status: str = "M"  # A, D, M, R
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    added_lines: set[int] = field(default_factory=set)
    context_lines: set[int] = field(default_factory=set)
    additions: int = 0
    deletions: int = 0
    header: list[str] = field(default_factory=list)

    @property
    def new_side_lines(self) -> set[int]:
        return self.added_lines | self.context_lines

    def text(self) -> str:
        out = list(self.header)
        for h in self.hunks:
            out.append(f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@{h.header}")
            out.extend(h.lines)
        return "\n".join(out)


def parse_unified_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_no = 0
    in_header = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            cur = FileDiff(path=_paths_from_git_header(raw)[1])
            cur.header.append(raw)
            files.append(cur)
            hunk = None
            in_header = True
            continue
        if cur is None:
            continue
        if in_header:
            if raw.startswith("@@"):
                in_header = False
            else:
                cur.header.append(raw)
                if raw.startswith("new file mode"):
                    cur.status = "A"
                elif raw.startswith("deleted file mode"):
                    cur.status = "D"
                elif raw.startswith("rename from "):
                    cur.status = "R"
                    cur.old_path = raw[len("rename from ") :]
                elif raw.startswith("rename to "):
                    cur.path = raw[len("rename to ") :]
                elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                    cur.binary = True
                elif raw.startswith("+++ "):
                    target = raw[4:].split("\t")[0]
                    if target.startswith("b/"):
                        cur.path = target[2:]
                    elif target == "/dev/null":
                        cur.status = "D"
                elif raw.startswith("--- "):
                    source = raw[4:].split("\t")[0]
                    if source.startswith("a/") and cur.old_path is None and cur.status != "A":
                        cur.old_path = source[2:]
                continue
        m = _HUNK.match(raw)
        if m:
            hunk = Hunk(
                old_start=int(m[1]),
                old_len=int(m[2] or 1),
                new_start=int(m[3]),
                new_len=int(m[4] or 1),
                header=m[5],
            )
            cur.hunks.append(hunk)
            new_no = hunk.new_start
            continue
        if hunk is None:
            continue
        hunk.lines.append(raw)
        if raw.startswith("+"):
            cur.added_lines.add(new_no)
            cur.additions += 1
            new_no += 1
        elif raw.startswith("-"):
            cur.deletions += 1
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            cur.context_lines.add(new_no)
            new_no += 1
    for f in files:
        if f.old_path is None and f.status in ("M", "D"):
            f.old_path = f.path
    return files


def _paths_from_git_header(line: str) -> tuple[str, str]:
    # "diff --git a/x b/x" -- paths with spaces are rare in Zephyr; split on " b/" from the right.
    rest = line[len("diff --git ") :]
    idx = rest.rfind(" b/")
    if idx == -1:
        return rest, rest
    return rest[2:idx], rest[idx + 3 :]


def changed_ranges(fd: FileDiff) -> list[tuple[int, int]]:
    """Contiguous ranges of added lines on the new side."""
    if not fd.added_lines:
        return []
    nums = sorted(fd.added_lines)
    ranges: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((start, prev))
        start = prev = n
    ranges.append((start, prev))
    return ranges
