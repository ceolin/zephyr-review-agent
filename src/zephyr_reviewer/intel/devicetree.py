# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Devicetree bindings and their users."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .repo import GitRepo


@dataclass(frozen=True)
class Binding:
    path: str
    compatible: str
    text: str


def bindings_for_compatible(repo: GitRepo, compatible: str, max_bytes: int = 40_000) -> list[Binding]:
    hits, _ = repo.grep(
        rf'^[[:space:]]*compatible:[[:space:]]*"{re.escape(compatible)}"[[:space:]]*$',
        paths="dts/bindings/**/*.y*ml",
        regex=True,
        max_results=50,
    )
    out: list[Binding] = []
    seen: set[str] = set()
    for h in hits:
        if h.path in seen:
            continue
        seen.add(h.path)
        text = repo.show(h.path, max_bytes=max_bytes) or ""
        out.append(Binding(h.path, compatible, text))
    return out


def bindings_by_name(repo: GitRepo, fragment: str, limit: int = 40) -> list[str]:
    frag = fragment.lower()
    return [p for p in repo.files() if p.startswith("dts/bindings/") and frag in p.lower()][:limit]


def compatible_users(repo: GitRepo, compatible: str, max_results: int = 300) -> dict[str, list[tuple[str, int, str]]]:
    """Where a compatible is instantiated (dts/dtsi/overlay) and where drivers bind to it (DT_DRV_COMPAT)."""
    users: dict[str, list[tuple[str, int, str]]] = {"devicetree": [], "drivers": [], "other": []}
    hits, _ = repo.grep(re.escape(compatible), regex=True, max_results=max_results * 2)
    drv_compat = compatible.replace(",", "_").replace("-", "_")
    for h in hits:
        if h.path.startswith("dts/bindings/"):
            continue
        if h.path.endswith((".dts", ".dtsi", ".overlay")):
            users["devicetree"].append((h.path, h.line, h.text.strip()))
        else:
            users["other"].append((h.path, h.line, h.text.strip()))
    drv_hits, _ = repo.grep(rf"DT_DRV_COMPAT[[:space:]]+{re.escape(drv_compat)}", regex=True, word=True, max_results=50)
    users["drivers"] = [(h.path, h.line, h.text.strip()) for h in drv_hits]
    for k in users:
        users[k] = users[k][:max_results]
    return users
