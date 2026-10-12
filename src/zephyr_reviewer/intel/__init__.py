# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Source intelligence over a Zephyr checkout.

Every query is pinned to a commit and answered with git plumbing, so nothing
from the tree under review is ever executed. That property is what makes it
safe to run against a fork's pull request head, and it is also what will let
the same tool surface be served by a shared, indexed service later on.
"""

from .service import SourceIntel

__all__ = ["SourceIntel"]
