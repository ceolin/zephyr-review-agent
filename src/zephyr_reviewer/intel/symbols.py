# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Definition and reference lookup for C, Kconfig, devicetree and CMake names.

Heuristic and regex based on purpose: it runs in seconds on a fresh checkout
with no index, which is what a per-PR server needs. A shared service can swap
in a real index behind the same ``find_definitions`` / ``find_references``
contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .repo import GitRepo, GrepHit


@dataclass(frozen=True)
class Definition:
    kind: str  # function, prototype, macro, struct, union, enum, typedef, variable, kconfig, dt-compatible, cmake
    path: str
    line: int
    text: str


_C_EXTS = (".c", ".h", ".cpp", ".hpp", ".cc", ".S", ".ld", ".cmake")
_C_KEYWORDS = {"return", "if", "else", "while", "for", "switch", "case", "goto", "sizeof", "do", "typedef"}
_IDENT = r"[A-Za-z_]\w*"
# Tokens of a declaration-specifier prefix. Tokenizing and checking each token is linear; the previous
# single regex of the form ((ident|*)\s*)+$ could split one identifier into many and backtracked
# exponentially on lines it failed to match, freezing the server (the regex engine holds the GIL).
_SPEC_TOKEN = re.compile(r"[A-Za-z_]\w*|\*|\S")


def _is_specifier_prefix(before: str) -> bool:
    """True when ``before`` consists only of identifiers, qualifiers and '*' (a plausible return type)."""
    if len(before) > 300:
        return False
    tokens = _SPEC_TOKEN.findall(before)
    return bool(tokens) and all(t == "*" or (t[0].isalpha() or t[0] == "_") for t in tokens)


def _looks_like_c_definition(name: str, text: str) -> str | None:
    """Classify a C line that mentions ``name`` followed by '(' as function/prototype/None."""
    stripped = text.strip()
    if stripped.startswith(("*", "//", "/*", "#", "}", "{")) or stripped.endswith("\\"):
        return None
    m = re.search(rf"\b{re.escape(name)}\s*\(", text)
    if not m:
        return None
    before = text[: m.start()]
    if not _is_specifier_prefix(before):
        return None
    tokens = re.findall(_IDENT, before)
    if not tokens or any(t in _C_KEYWORDS for t in tokens):
        return None
    if stripped.endswith(";"):
        return "prototype"
    if "=" in before or "->" in before:
        return None
    return "function"


def _kind_of(name: str, hit: GrepHit) -> str | None:
    text = hit.text
    stripped = text.strip()
    path = hit.path
    base = path.rsplit("/", 1)[-1]
    esc = re.escape(name)

    if base.startswith("Kconfig") or base.endswith(".defconfig"):
        if re.match(rf"^\s*(menu)?config\s+{esc}\s*$", text):
            return "kconfig"
        return None
    if path.startswith("dts/bindings/") and path.endswith((".yaml", ".yml")):
        if re.search(rf'compatible:\s*"{esc}"', text):
            return "dt-compatible"
        return None
    if base == "CMakeLists.txt" or base.endswith(".cmake"):
        if re.match(rf"^\s*(function|macro)\s*\(\s*{esc}\b", text):
            return "cmake"
        return None
    if not path.endswith(_C_EXTS):
        return None
    if stripped.startswith(("*", "//", "/*")):
        return None

    if re.match(rf"^\s*#\s*define\s+{esc}\b", text):
        return "macro"
    m = re.match(rf"^\s*(?:typedef\s+)?(struct|union|enum)\s+{esc}\s*(\{{|$)", text)
    if m:
        return m.group(1)
    if re.match(rf"^\s*typedef\b.*\b{esc}\s*(;|\)|\[)", text):
        return "typedef"
    kind = _looks_like_c_definition(name, text)
    if kind:
        return kind
    m = re.match(rf"^(?P<before>[A-Za-z_][\w\s\*]*)\b{esc}\s*(\[[^\]]*\])?\s*(=|;)", text)
    if m and not any(t in _C_KEYWORDS for t in re.findall(_IDENT, m.group("before"))):
        return "variable"
    return None


def find_definitions(repo: GitRepo, name: str, *, paths: str | None = None, max_scan: int = 4000) -> list[Definition]:
    """Where ``name`` is defined. Accepts CONFIG_FOO (Kconfig) and vendor,compat (devicetree) too."""
    name = name.strip()
    if not name:
        return []
    queries: list[tuple[str, bool]] = []  # (pattern, word)
    if name.startswith("CONFIG_"):
        queries.append((name[len("CONFIG_") :], True))
    if "," in name and " " not in name:
        queries.append((name, False))
    queries.append((name, True))

    defs: list[Definition] = []
    seen: set[tuple[str, int]] = set()
    for pattern, word in queries:
        hits, _ = repo.grep(pattern, paths=paths, regex=False, word=word, max_results=max_scan)
        for hit in hits:
            kind = _kind_of(pattern, hit)
            if kind and (hit.path, hit.line) not in seen:
                seen.add((hit.path, hit.line))
                defs.append(Definition(kind, hit.path, hit.line, hit.text.strip()))
    if not any(d.kind == "function" for d in defs) and not name.startswith(("z_impl_", "CONFIG_")):
        # Zephyr syscalls: the header declares k_foo(), the implementation is z_impl_k_foo().
        hits, _ = repo.grep("z_impl_" + name, paths=paths, regex=False, word=True, max_results=200)
        for hit in hits:
            kind = _kind_of("z_impl_" + name, hit)
            if kind == "function" and (hit.path, hit.line) not in seen:
                seen.add((hit.path, hit.line))
                defs.append(Definition("function (syscall impl)", hit.path, hit.line, hit.text.strip()))
    order = {
        "function": 0,
        "function (syscall impl)": 0,
        "macro": 1,
        "struct": 2,
        "union": 2,
        "enum": 2,
        "typedef": 3,
        "kconfig": 4,
        "dt-compatible": 5,
        "cmake": 6,
        "variable": 7,
        "prototype": 8,
    }
    defs.sort(key=lambda d: (order.get(d.kind, 9), d.path, d.line))
    return defs


@dataclass
class References:
    hits: list[GrepHit]
    truncated: bool
    by_dir: dict[str, int]


def find_references(
    repo: GitRepo, name: str, *, paths: str | None = None, max_results: int = 2000, exclude_defs: bool = True
) -> References:
    pattern = name[len("CONFIG_") :] if name.startswith("CONFIG_") and not paths else name
    hits, truncated = repo.grep(pattern, paths=paths, regex=False, word=True, max_results=max_results)
    if name.startswith("CONFIG_"):
        # Keep both CONFIG_FOO in C/CMake and bare FOO in Kconfig files.
        hits = [h for h in hits if "CONFIG_" + pattern in h.text or h.path.rsplit("/", 1)[-1].startswith("Kconfig")]
    if exclude_defs:
        hits = [h for h in hits if _kind_of(pattern, h) is None]
    by_dir: dict[str, int] = {}
    for h in hits:
        top = "/".join(h.path.split("/")[:2])
        by_dir[top] = by_dir.get(top, 0) + 1
    return References(hits, truncated, dict(sorted(by_dir.items(), key=lambda kv: -kv[1])))
