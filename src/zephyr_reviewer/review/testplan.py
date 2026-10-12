# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Validate a model-written test plan against the tree: real suites, real scenarios, real platforms."""

from __future__ import annotations

import difflib
from typing import Any

import yaml

from ..intel import SourceIntel, tests_index
from .schema import Review, TestPlanEntry


def annotate(review: Review, intel: SourceIntel) -> None:
    """Attach notes to each entry: unknown suite, scenario or platform; platforms the suite excludes."""
    idx = intel.platform_index
    for e in review.test_plan:
        suite = tests_index.suite_for_dir(intel.head, e.suite, "")
        if suite is None:
            e.notes.append("suite directory has no tests.yaml / sample.yaml at the head commit")
            continue
        if e.scenario and e.scenario not in suite.scenarios:
            close = difflib.get_close_matches(e.scenario, suite.scenarios, n=1, cutoff=0.6)
            hint = f"; did you mean {close[0]}?" if close else ""
            e.notes.append(f"scenario {e.scenario!r} is not in {suite.meta_path}{hint}")
        for p in e.platforms:
            if not idx.exists(p):
                e.notes.append(f"unknown platform identifier {p!r}")
            elif suite.platform_allow and not any(_allows(a, p) for a in suite.platform_allow):
                e.notes.append(f"{p} is outside the suite's platform_allow")
            elif p in suite.platform_exclude:
                e.notes.append(f"{p} is in the suite's platform_exclude")


def _allows(allow_entry: str, platform: str) -> bool:
    """platform_allow entries are exact identifiers; a bare board name covers that board's default SoC."""
    return platform == allow_entry or ("/" not in allow_entry and platform.startswith(allow_entry + "/"))


def to_yaml(review: Review, head_sha: str) -> str:
    data: dict[str, Any] = {
        "head": head_sha,
        "entries": [
            {
                "suite": e.suite,
                **({"scenario": e.scenario} if e.scenario else {}),
                "platforms": e.platforms,
                "priority": e.priority,
                **({"build_only": True} if e.build_only else {}),
                "reason": e.reason,
                **({"notes": e.notes} if e.notes else {}),
                "twister": e.twister(),
            }
            for e in review.test_plan
        ],
    }
    return yaml.safe_dump(data, sort_keys=False, width=100)


def commands(entries: list[TestPlanEntry]) -> list[str]:
    return [e.twister() for e in entries if not any("unknown" in n or "has no tests.yaml" in n for n in e.notes)]
