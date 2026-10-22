# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Prefetched context: enclosing functions, windows, callers, budget, and its place in the opening message."""

from __future__ import annotations

from zephyr_reviewer.intel import SourceIntel
from zephyr_reviewer.intel.prefetch import _is_function_header, enclosing_function
from zephyr_reviewer.review.config import ReviewConfig
from zephyr_reviewer.review.github_api import PullRequest
from zephyr_reviewer.review.prompts import opening_message

C_SRC = """\
/* SPDX */
#include <zephyr/kernel.h>

#define FOO(x) do { (x)++; } while (0)

struct bar {
	int a;
};

static int helper(int v);

static int helper(int v)
{
	const char *s = "{ not a brace }";
	if (v > '{') {
		return v;
	}
	return 0; /* } in comment */
}

int ISR_DIRECT_DECLARE(foo_isr)
{
	return 1;
}

static int long_params(const struct device *dev,
                       struct i2c_msg *msgs,
                       uint8_t num)
{
	FOO(num);
	return helper(num);
}

int tail(void) { return 0; }
""".splitlines()


def test_function_headers_and_enclosing_spans():
    assert _is_function_header("static int helper(int v)") == "helper"
    assert _is_function_header("static int helper(int v);") is None  # prototype
    assert _is_function_header("\treturn helper(num);") is None  # indented
    assert _is_function_header("if (v > 0) {") is None
    assert _is_function_header("#define FOO(x) do { (x)++; } while (0)") is None
    assert _is_function_header("int tail(void) { return 0; }") == "tail"

    # Braces in strings, chars and comments do not confuse the span.
    assert enclosing_function(C_SRC, 15) == (12, 19, "helper")
    assert enclosing_function(C_SRC, 14) == (12, 19, "helper")
    # A multi-line parameter list still belongs to its function.
    assert enclosing_function(C_SRC, 30) == (26, 32, "long_params")
    assert enclosing_function(C_SRC, 27) == (26, 32, "long_params")
    # One-line function.
    assert enclosing_function(C_SRC, 34) == (34, 34, "tail")
    # Outside any function: a macro, a struct member, a prototype, a blank line.
    assert enclosing_function(C_SRC, 4) is None
    assert enclosing_function(C_SRC, 7) is None
    assert enclosing_function(C_SRC, 10) is None
    assert enclosing_function(C_SRC, 20) is None


def test_prefetch_blocks_on_the_fixture(mini_repo):
    intel = SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])
    text = intel.prefetch(24_000)
    assert text.startswith(f"## Prefetched context (head {mini_repo['head'][:12]})")
    heads = [ln for ln in text.splitlines() if ln.startswith("#### ")]
    assert heads == [
        "#### whole file (drivers/i2c/Kconfig:1-22, 5 changed line(s))",
        "#### foo_transfer (drivers/i2c/i2c_foo.c:10-22, 3 changed line(s))",
        "#### foo_recover (drivers/i2c/i2c_foo.c:32-35, 4 changed line(s))",
        "#### whole file (tests/drivers/i2c/foo/tests.yaml:1-12, 2 changed line(s))",
    ]
    assert "   17+| \tif (ret < 0) {" in text and "   15 | \tk_sem_take" in text
    assert "   32+| int foo_recover(const struct device *dev)" in text
    assert "Callers of foo_transfer: none found outside drivers/i2c/i2c_foo.c." in text
    assert "outside any function" not in text  # the blank added line did not create a window
    assert "Omitted" not in text

    # Budget: the most-changed blocks are kept, the rest are named.
    small = intel.prefetch(900)
    assert "#### whole file (drivers/i2c/Kconfig" in small
    assert "Omitted for size (3 block(s))" in small and "foo_transfer" in small.split("Omitted")[1]
    assert (
        intel.prefetch(0) == ""
        and SourceIntel(str(mini_repo["root"]), mini_repo["base"], mini_repo["base"]).prefetch(1000) == ""
    )


def test_opening_message_carries_prefetched_context_after_the_diff():
    pr = PullRequest(1, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    text = opening_message(pr, "ov", "diff text", "", "o/r", prefetched="## Prefetched context (head x)\nstuff")
    assert text.index("### Full diff") < text.index("### Prefetched context") < text.index("Investigate with the tools")
    assert "### Prefetched context" not in opening_message(pr, "ov", None, "", "o/r", prefetched="")


def test_prefetch_budget_is_configurable(monkeypatch):
    assert ReviewConfig().prefetch_chars == 24_000
    monkeypatch.setenv("INPUT_PREFETCH_CHARS", "0")
    assert ReviewConfig.from_env().prefetch_chars == 0
