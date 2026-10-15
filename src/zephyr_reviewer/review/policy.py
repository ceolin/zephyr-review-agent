# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Safety settings that a request may tighten but never relax.

The operator sets a floor (workflow inputs today; server flags for the shared service later). A
request (CLI flags today; per-request headers later) may ask for stricter values. Anything looser
is ignored and reported. This mirrors the GitHub MCP server's lockdown header semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

LOCKDOWN_LEVELS = ["off", "label", "strict"]


@dataclass(frozen=True)
class Policy:
    lockdown: str = "label"  # off < label < strict
    max_steps: int = 40
    max_context_tokens: int = 160_000
    max_tool_result_chars: int = 24_000
    post_comment: bool = True
    post_inline: bool = False
    dry_run: bool = False

    def tighten(self, **requested) -> tuple[Policy, list[str]]:
        """Apply requested values that are at least as safe as the floor; return (policy, rejected)."""
        new = self
        rejected: list[str] = []
        for key, value in requested.items():
            if value is None:
                continue
            if key == "lockdown":
                if LOCKDOWN_LEVELS.index(value) >= LOCKDOWN_LEVELS.index(self.lockdown):
                    new = replace(new, lockdown=value)
                else:
                    rejected.append(f"lockdown={value} is looser than the operator floor {self.lockdown}")
            elif key in ("max_steps", "max_context_tokens", "max_tool_result_chars"):
                floor = getattr(self, key)
                if int(value) <= floor:
                    new = replace(new, **{key: int(value)})
                else:
                    rejected.append(f"{key}={value} exceeds the operator floor {floor}")
            elif key in ("post_comment", "post_inline"):
                floor = getattr(self, key)
                if not value or floor:
                    new = replace(new, **{key: bool(value)})
                else:
                    rejected.append(f"{key} is disabled by the operator")
            elif key == "dry_run":
                if value or not self.dry_run:
                    new = replace(new, dry_run=bool(value))
                else:
                    rejected.append("dry_run is enforced by the operator")
            else:
                raise KeyError(key)
        return new, rejected
