# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Deterministic context for the opening message: the functions each hunk lands in, and their callers.

Read from git objects at the head commit and labelled with its SHA. Models issue tool calls one at a
time, so every lookup the model does not have to make saves a round trip over the whole transcript;
this is the cheapest cost reduction available. Bounded by a character budget, most-changed functions
first; whatever does not fit is named so the model can fetch it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import symbols
from .diff import FileDiff
from .repo import GitRepo

_C_EXTS = (".c", ".h", ".cpp", ".hpp", ".cc")
_SMALL_WHOLE_EXTS = (".yaml", ".yml", ".dts", ".dtsi", ".overlay", ".conf", ".cmake")
_IDENT = r"[A-Za-z_]\w*"
_HEADER = re.compile(rf"^(?P<spec>(?:{_IDENT}|\*)(?:[\s*]+(?:{_IDENT}|\*))*)\s+\**\s*(?P<name>{_IDENT})\s*\(")
_KEYWORDS = {"if", "else", "while", "for", "switch", "return", "do", "case", "goto", "sizeof", "typedef"}
MAX_FUNCTION_LINES = 160
WINDOW = 25


@dataclass
class Block:
    path: str
    start: int
    end: int
    name: str  # function name, or a label such as "lines 10-30"
    kind: str  # function | window | file
    changed: int
    lines: list[str] = field(default_factory=list)
    added: set[int] = field(default_factory=set)
    callers: str = ""

    def render(self) -> str:
        head = f"#### {self.name} ({self.path}:{self.start}-{self.end}, {self.changed} changed line(s))"
        body = [
            f"{n:5d}{'+' if n in self.added else ' '}| {ln}"
            for n, ln in zip(range(self.start, self.end + 1), self.lines, strict=False)
        ]
        out = [head, *body]
        if self.callers:
            out.append(self.callers)
        return "\n".join(out)

    @property
    def chars(self) -> int:
        return len(self.render()) + 2


def _is_function_header(line: str) -> str | None:
    """Name of the function a column-0 line defines, else None."""
    if not line or line[0].isspace() or line.startswith(("#", "/", "*", "}", "{")):
        return None
    m = _HEADER.match(line)
    if not m:
        return None
    tokens = re.findall(_IDENT, m.group("spec"))
    if not tokens or any(t in _KEYWORDS for t in tokens) or m.group("name") in _KEYWORDS:
        return None
    stripped = line.rstrip()
    if stripped.endswith(";") or "=" in m.group("spec"):
        return None
    return m.group("name")


def enclosing_function(lines: list[str], line_no: int) -> tuple[int, int, str] | None:
    """(start, end, name) of the C function containing 1-based ``line_no``, found by header and brace matching."""
    i = min(max(line_no, 1), len(lines)) - 1
    # Walk up to a column-0 header whose body opens at or before our line.
    for s in range(i, -1, -1):
        name = _is_function_header(lines[s])
        if name is None:
            continue
        # The body opens at the first '{' at column 0 or at the end of the header/parameter lines.
        depth = 0
        opened = False
        for e in range(s, len(lines)):
            for ch in _strip_literals(lines[e]):
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                if e >= i:
                    return s + 1, e + 1, name
                break  # this function ended before our line; keep walking up
            if not opened and e - s > 12:
                break  # a prototype or macro spanning lines, not a definition
        if s < i and not opened:
            continue
    return None


def _strip_literals(line: str) -> str:
    """Drop string and char literals and // comments so their braces do not count."""
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif line.startswith("//", i):
            break
        elif line.startswith("/*", i):
            j = line.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def blocks_for_file(repo: GitRepo, fd: FileDiff) -> list[Block]:
    if fd.binary or fd.status == "D" or not fd.added_lines:
        return []
    text = repo.show(fd.path)
    if text is None:
        return []
    lines = text.splitlines()
    total = len(lines)
    blocks: list[Block] = []
    if fd.path.endswith(_C_EXTS):
        # Pass 1: which function, if any, each added line lands in. Whitespace-only added lines carry
        # nothing and would only widen windows, so they are ignored.
        spans: dict[tuple[int, int], tuple[str, list[int]]] = {}
        loose: list[int] = []
        for n in sorted(fd.added_lines):
            if n > total or not lines[n - 1].strip():
                continue
            found = enclosing_function(lines, n)
            if found:
                s, e, name = found
                spans.setdefault((s, e), (name, []))[1].append(n)
            else:
                loose.append(n)
        # Pass 2: one block per function, excerpted when very long.
        for (s, e), (name, changed) in sorted(spans.items()):
            if e - s + 1 > MAX_FUNCTION_LINES:
                lo, hi = max(s, min(changed) - WINDOW), min(e, max(changed) + WINDOW)
                blocks.append(
                    Block(
                        fd.path,
                        lo,
                        hi,
                        f"{name} (excerpt; function spans {s}-{e})",
                        "function",
                        len(changed),
                        lines[lo - 1 : hi],
                        set(changed),
                    )
                )
            else:
                blocks.append(Block(fd.path, s, e, name, "function", len(changed), lines[s - 1 : e], set(changed)))
        # Lines outside any function (globals, macros, struct members, declarations): small windows.
        covered = {n for (s, e) in spans for n in range(s, e + 1)}
        loose = [n for n in loose if n not in covered]
        for lo, hi in _windows(loose, total, 8):
            changed = sorted(x for x in fd.added_lines if lo <= x <= hi)
            blocks.append(
                Block(
                    fd.path,
                    lo,
                    hi,
                    f"lines {lo}-{hi} (outside any function)",
                    "window",
                    len(changed),
                    lines[lo - 1 : hi],
                    set(changed),
                )
            )
    elif fd.path.endswith(_SMALL_WHOLE_EXTS) or fd.path.rsplit("/", 1)[-1].startswith("Kconfig"):
        if total <= 120:
            blocks.append(
                Block(fd.path, 1, total, "whole file", "file", len(fd.added_lines), lines, set(fd.added_lines))
            )
        else:
            for lo, hi in _windows(sorted(fd.added_lines), total, 12):
                changed = [x for x in fd.added_lines if lo <= x <= hi]
                blocks.append(
                    Block(fd.path, lo, hi, f"lines {lo}-{hi}", "window", len(changed), lines[lo - 1 : hi], set(changed))
                )
    return blocks


def _windows(nums: list[int], total: int, pad: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in nums:
        lo, hi = max(1, n - pad), min(total, n + pad)
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def callers_line(repo: GitRepo, name: str, own_path: str, limit: int = 6) -> str:
    refs = symbols.find_references(repo, name, max_results=400)
    sites = [h for h in refs.hits if not (h.path == own_path)]
    if not sites:
        return f"Callers of {name}: none found outside {own_path}."
    by_dir = ", ".join(f"{d} ({n})" for d, n in list(refs.by_dir.items())[:5])
    examples = "; ".join(f"{h.path}:{h.line}" for h in sites[:limit])
    more = f"{'+' if refs.truncated else ''}" if refs.truncated else ""
    return f"Callers of {name}: {len(sites)}{more} site(s) in {by_dir}; e.g. {examples}."


def prefetch(repo: GitRepo, files: list[FileDiff], budget_chars: int, *, with_callers: bool = True) -> str:
    if budget_chars <= 0:
        return ""
    blocks: list[Block] = []
    for fd in files:
        blocks.extend(blocks_for_file(repo, fd))
    if not blocks:
        return ""
    if with_callers:
        for b in blocks:
            if b.kind == "function" and not b.name.startswith("lines"):
                fname = b.name.split(" ", 1)[0]
                if fname and not fname.startswith(("main", "ZTEST")):
                    b.callers = callers_line(repo, fname, b.path)
    blocks.sort(key=lambda b: (-b.changed, b.path, b.start))
    header = (
        f"## Prefetched context (head {repo.sha[:12]})\n"
        "The functions each hunk lands in, read at the head commit, with lines added by this change marked '+'. "
        "Use read_file for anything listed as omitted or for more context.\n"
    )
    used = len(header)
    kept: list[Block] = []
    omitted: list[Block] = []
    for b in blocks:
        if used + b.chars <= budget_chars:
            kept.append(b)
            used += b.chars
        else:
            omitted.append(b)
    kept.sort(key=lambda b: (b.path, b.start))
    out = [header]
    current = None
    for b in kept:
        if b.path != current:
            out.append(f"### {b.path}")
            current = b.path
        out.append(b.render())
        out.append("")
    if omitted:
        names = "; ".join(f"{b.path}:{b.start}-{b.end} {b.name}" for b in omitted[:12])
        more = f" (+{len(omitted) - 12} more)" if len(omitted) > 12 else ""
        out.append(f"Omitted for size ({len(omitted)} block(s)): {names}{more}.")
    return "\n".join(out).rstrip() + "\n"
