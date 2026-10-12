# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Kconfig symbol lookup: every definition block of a symbol, and who uses it."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .repo import GitRepo

_BLOCK_END = re.compile(
    r"^(menuconfig|config|choice|endchoice|menu|endmenu|if\s|endif|source|rsource|osource|orsource|comment|mainmenu)\b"
)


@dataclass(frozen=True)
class KconfigDef:
    path: str
    line: int
    block: str


def _symbol(name: str) -> str:
    name = name.strip()
    return name[len("CONFIG_") :] if name.startswith("CONFIG_") else name


def definitions(repo: GitRepo, name: str) -> list[KconfigDef]:
    sym = _symbol(name)
    hits, _ = repo.grep(
        rf"^[[:space:]]*(menu)?config[[:space:]]+{re.escape(sym)}[[:space:]]*$",
        paths="**/Kconfig*",
        regex=True,
        max_results=200,
    )
    out: list[KconfigDef] = []
    cache: dict[str, list[str]] = {}
    for hit in hits:
        if hit.path not in cache:
            cache[hit.path] = (repo.show(hit.path) or "").splitlines()
        lines = cache[hit.path]
        start = hit.line - 1
        end = start + 1
        while end < len(lines):
            if lines[end] and not lines[end][0].isspace() and _BLOCK_END.match(lines[end]):
                break
            end += 1
        # Trim trailing blank lines.
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        out.append(KconfigDef(hit.path, hit.line, "\n".join(lines[start:end])))
    return out


def references(repo: GitRepo, name: str, max_results: int = 400) -> dict[str, list[tuple[str, int, str]]]:
    """References grouped as kconfig / config-files / cmake / source."""
    sym = _symbol(name)
    groups: dict[str, list[tuple[str, int, str]]] = {"kconfig": [], "config": [], "cmake": [], "source": []}
    hits, _ = repo.grep(rf"(CONFIG_)?{re.escape(sym)}", regex=True, word=True, max_results=max_results * 3)
    for h in hits:
        base = h.path.rsplit("/", 1)[-1]
        text = h.text.strip()
        if base.startswith("Kconfig"):
            if re.match(rf"^(menu)?config\s+{re.escape(sym)}\s*$", text):
                continue
            if sym not in text:
                continue
            groups["kconfig"].append((h.path, h.line, text))
        elif base.endswith((".conf", "_defconfig", ".defconfig")) or base == "defconfig":
            if "CONFIG_" + sym in text:
                groups["config"].append((h.path, h.line, text))
        elif base == "CMakeLists.txt" or base.endswith(".cmake"):
            if "CONFIG_" + sym in text:
                groups["cmake"].append((h.path, h.line, text))
        elif "CONFIG_" + sym in text:
            groups["source"].append((h.path, h.line, text))
    for key in groups:
        groups[key] = groups[key][:max_results]
    return groups
