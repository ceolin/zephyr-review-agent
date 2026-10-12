# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Test-plan support: platform index, candidate gathering, schema, validation and rendering."""

from __future__ import annotations

import copy

import pytest
import yaml

from zephyr_reviewer.intel import SourceIntel, tests_index
from zephyr_reviewer.intel.platforms import PlatformIndex, _identifiers
from zephyr_reviewer.intel.repo import GitRepo
from zephyr_reviewer.review import testplan
from zephyr_reviewer.review.github_api import PullRequest
from zephyr_reviewer.review.prompts import opening_message
from zephyr_reviewer.review.render import render_comment
from zephyr_reviewer.review.schema import parse_review


@pytest.fixture(scope="module")
def intel(mini_repo) -> SourceIntel:
    return SourceIntel(str(mini_repo["root"]), mini_repo["head"], mini_repo["base"])


def test_identifiers_from_dts_names_and_board_yml():
    assert _identifiers(
        "nrf5340dk",
        ["nrf5340"],
        ["nrf5340dk_nrf5340_cpuapp", "nrf5340dk_nrf5340_cpuapp_ns", "nrf5340dk_nrf5340_cpunet"],
    ) == ["nrf5340dk/nrf5340/cpuapp", "nrf5340dk/nrf5340/cpuapp/ns", "nrf5340dk/nrf5340/cpunet"]
    assert _identifiers("nrf52840dk", ["nrf52840", "nrf52811"], ["nrf52840dk_nrf52840", "nrf52840dk_nrf52811"]) == [
        "nrf52840dk/nrf52811",
        "nrf52840dk/nrf52840",
    ]
    assert _identifiers("native_sim", ["native"], ["native_sim_native", "native_sim_native_64"]) == [
        "native_sim/native",
        "native_sim/native/64",
    ]
    assert _identifiers("b", ["soc_a"], []) == ["b/soc_a"]  # no dts: fall back to board/soc


def test_platform_index_over_the_fixture(mini_repo):
    idx = PlatformIndex.build(GitRepo(mini_repo["root"], mini_repo["head"]))
    assert sorted(idx.by_identifier) == [
        "native_sim/native",
        "native_sim/native/64",
        "vndboard/vndsoc/cpuapp",
        "vndboard/vndsoc/cpuapp/ns",
    ]
    assert idx.for_path("boards/vnd/vndboard/vndboard_vndsoc_cpuapp.dts") == [
        "vndboard/vndsoc/cpuapp",
        "vndboard/vndsoc/cpuapp/ns",
    ]
    assert idx.for_path("drivers/i2c/i2c_foo.c") == []
    assert idx.search("native") == ["native_sim/native", "native_sim/native/64"]
    assert idx.search("Vendor Board") == ["vndboard/vndsoc/cpuapp", "vndboard/vndsoc/cpuapp/ns"]
    assert idx.exists("vndboard/vndsoc/cpuapp") and not idx.exists("vndboard/vndsoc")


def test_show_many_batches_reads(mini_repo):
    repo = GitRepo(mini_repo["root"], mini_repo["head"])
    got = repo.show_many(["MAINTAINERS.yml", "nope.c", "drivers/i2c/Kconfig"])
    assert set(got) == {"MAINTAINERS.yml", "drivers/i2c/Kconfig"}
    assert got["drivers/i2c/Kconfig"].startswith("# SPDX")


def test_changed_suite_dirs_and_common_rule(mini_repo):
    repo = GitRepo(mini_repo["root"], mini_repo["head"])
    owned = tests_index.changed_suite_dirs(
        repo,
        [
            "tests/drivers/i2c/foo/src/main.c",
            "tests/drivers/i2c/common/helpers.h",
            "drivers/i2c/i2c_foo.c",
            "tests/kernel/semaphore/README.rst",
        ],
    )
    assert owned == {
        "tests/drivers/i2c/foo": ["tests/drivers/i2c/foo/src/main.c", "tests/drivers/i2c/common/helpers.h"],
        "tests/drivers/i2c/target": ["tests/drivers/i2c/common/helpers.h"],
    }


def test_tags_and_tagged_suites(mini_repo):
    repo = GitRepo(mini_repo["root"], mini_repo["head"])
    tags = tests_index.Tags.from_repo(repo)
    assert tags is not None
    assert tags.for_paths(["drivers/i2c/i2c_foo.c", "kernel/sem.c", "doc/x.rst"]) == ["i2c", "kernel"]
    suites = tests_index.suites_with_tags(repo, ["kernel"])
    assert [s.meta_path for s in suites] == ["tests/kernel/semaphore/tests.yaml"]
    s = suites[0]
    assert s.platform_allow == ["native_sim", "vndboard/vndsoc"] and s.build_only and s.why == "tagged kernel"


def test_test_candidates_tool_text(intel: SourceIntel):
    text = intel.test_candidates()
    head, rest = text.split("## Test identifiers", 1)
    assert (
        "## tests/drivers/i2c/foo  (tests/drivers/i2c/foo/tests.yaml; edits tests/drivers/i2c/foo/tests.yaml)" in head
    )
    assert "scenarios: drivers.i2c.foo, drivers.i2c.foo.recovery" in head
    assert "- Drivers: I2C: drivers.i2c" in rest
    assert "## Twister tags implicated (scripts/ci/tags.yaml): i2c" in rest
    assert "## tests/drivers/i2c/target" in rest  # under tests/drivers/i2c
    assert "devicetree nodes with compatible vnd,foo-i2c: vndboard/vndsoc/cpuapp, vndboard/vndsoc/cpuapp/ns" in rest
    assert "validate platform identifiers with list_platforms" in rest


def test_boards_including_follows_dtsi_chain(intel: SourceIntel):
    both = {"vndboard/vndsoc/cpuapp", "vndboard/vndsoc/cpuapp/ns"}
    assert intel._boards_including("dts/arm/vnd/vndsoc.dtsi") == both
    assert intel._boards_including("dts/arm/vnd/vndsoc_common.dtsi") == both  # one level of indirection
    assert intel._boards_including("dts/arm/vnd/vndsoc_common.dtsi", depth=1) == set()
    assert intel._boards_including("dts/arm/vnd/nothing.dtsi") == set()


def test_list_platforms_tool_text(intel: SourceIntel):
    assert "4 platform identifiers across 2 boards" in intel.platforms("")
    assert "vndboard/vndsoc/cpuapp  (Vendor Board; boards/vnd/vndboard)" in intel.platforms("vnd")
    assert "No platform identifier" in intel.platforms("stm32")


PLAN = [
    {
        "suite": "tests/drivers/i2c/foo",
        "scenario": "drivers.i2c.foo.recovery",
        "platforms": ["vndboard/vndsoc/cpuapp"],
        "reason": "Exercises foo_recover on the board that instantiates the driver.",
        "priority": "must",
    },
    {
        "suite": "tests/kernel/semaphore",
        "scenario": "tests/kernel/semaphore/kernel.semaphore",
        "platforms": "native_sim/native, vndboard/vndsoc/cpuapp/ns",
        "reason": "Lock semantics.",
        "priority": "SHOULD",
        "build_only": True,
    },
    {
        "suite": "tests/drivers/i2c/foo/",
        "scenario": "drivers.i2c.foo.recover",
        "platforms": ["qemu_x86"],
        "reason": "Typo'd scenario and unknown platform.",
        "priority": "optional",
    },
    {"suite": "tests/nowhere", "platforms": [], "reason": "Missing suite.", "priority": "nice"},
]


def test_parse_and_annotate_test_plan(intel: SourceIntel):
    review, errors = parse_review(
        {"summary": "s", "verdict": "approve", "confidence": "high", "findings": [], "test_plan": PLAN},
        {"drivers/i2c/i2c_foo.c": {17}},
    )
    assert not errors and review is not None and len(review.test_plan) == 4
    e0, e1, e2, e3 = review.test_plan
    assert e1.scenario == "kernel.semaphore"  # suite prefix stripped
    assert (
        e1.platforms == ["native_sim/native", "vndboard/vndsoc/cpuapp/ns"] and e1.priority == "should" and e1.build_only
    )
    assert e2.suite == "tests/drivers/i2c/foo" and e3.priority == "should"

    testplan.annotate(review, intel)
    assert e0.notes == []
    assert e1.notes == ["vndboard/vndsoc/cpuapp/ns is outside the suite's platform_allow"]
    assert e2.notes == [
        "scenario 'drivers.i2c.foo.recover' is not in tests/drivers/i2c/foo/tests.yaml; did you mean "
        "drivers.i2c.foo.recovery?",
        "unknown platform identifier 'qemu_x86'",
    ]
    assert e3.notes == ["suite directory has no tests.yaml / sample.yaml at the head commit"]

    assert e0.twister() == (
        "west twister -v --inline-logs -s tests/drivers/i2c/foo/drivers.i2c.foo.recovery -p vndboard/vndsoc/cpuapp"
    )
    assert e1.twister().startswith(
        "west twister -v --inline-logs --build-only -s tests/kernel/semaphore/kernel.semaphore"
    )
    assert testplan.commands(review.test_plan) == [e0.twister(), e1.twister()]  # broken entries get no command

    doc = yaml.safe_load(testplan.to_yaml(review, "abc123"))
    assert doc["head"] == "abc123" and doc["entries"][0]["twister"] == e0.twister()
    assert doc["entries"][2]["notes"] and "scenario" not in doc["entries"][3]


def test_render_test_plan_and_prompt_request(intel: SourceIntel, mini_repo):
    from tests.test_agent import GOOD_REVIEW
    from zephyr_reviewer.review.agent import RunResult
    from zephyr_reviewer.review.openrouter import Usage

    review, _ = parse_review(
        dict(copy.deepcopy(GOOD_REVIEW), test_plan=PLAN[:3]),
        {"drivers/i2c/i2c_foo.c": {17, 18, 19}, "drivers/i2c/Kconfig": {20}},
    )
    testplan.annotate(review, intel)
    pr = PullRequest(7, "t", "", "a", "main", "b" * 40, "pr", "h" * 40, "o/r", False)
    body = render_comment(review, RunResult(review, 1, Usage(), ["m"], False), pr, repo="o/r", tag="t")
    assert "### Test plan" in body
    assert "| 🔴 must | `tests/drivers/i2c/foo`<br>`drivers.i2c.foo.recovery` | `vndboard/vndsoc/cpuapp` |" in body
    assert "<sub>build only</sub>" in body and "⚠ scenario 'drivers.i2c.foo.recover' is not in" in body
    assert "```sh\nwest twister -v --inline-logs -s tests/drivers/i2c/foo/drivers.i2c.foo.recovery" in body

    plain = opening_message(pr, "ov", None, "", "o/r")
    asked = opening_message(pr, "ov", None, "", "o/r", None, True)
    assert "### Test plan requested" not in plain and "### Test plan requested" in asked and "test_candidates" in asked
