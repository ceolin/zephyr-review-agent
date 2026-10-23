# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""MCP server exposing Zephyr source intelligence for one pull request.

Runs over stdio (spawned per review) or streamable HTTP (a shared service).
The tool surface is the stable contract; see ``intel.service.SourceIntel``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from .intel import SourceIntel


# Typed outputs: these tools return human-readable text for the model *and* structured content for
# programmatic clients. The return annotation publishes the output schema; the function returns a
# CallToolResult carrying both.
class CommitOut(TypedDict):
    sha: str
    author: str
    subject: str


class ChangedFileOut(TypedDict):
    path: str
    status: str
    old_path: str | None
    additions: int
    deletions: int
    binary: bool
    areas: list[str]


class OverviewOut(TypedDict):
    base_sha: str
    head_sha: str
    shallow: bool
    commits: list[CommitOut]
    files: list[ChangedFileOut]


class FileGroupOut(TypedDict):
    name: str
    collaborators: list[str]


class AreaOut(TypedDict):
    name: str
    status: str
    maintainers: list[str]
    collaborators: list[str]
    labels: list[str]
    tests: list[str]
    file_groups: list[FileGroupOut]


class MaintainersOut(TypedDict):
    path: str
    areas: list[AreaOut]


class PlatformOut(TypedDict):
    identifier: str
    board: str
    full_name: str
    directory: str


class PlatformsOut(TypedDict):
    total_identifiers: int
    total_boards: int
    fragment: str
    platforms: list[PlatformOut]


class DefinitionOut(TypedDict):
    kind: str
    path: str
    line: int
    text: str


class DefinitionsOut(TypedDict):
    name: str
    definitions: list[DefinitionOut]


def _both(text: str, data: dict[str, Any]) -> Any:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


INSTRUCTIONS = """\
Source intelligence for a Zephyr RTOS pull request. Every tool answers against the
pull request's head commit (and base commit where noted); nothing is executed.
Start with pr_overview, read full files with read_file before judging a hunk, and use
find_definition / find_references / kconfig_symbol / dt_binding to verify assumptions
instead of guessing. Results are plain text, bounded in size; narrow a query when told so.\
"""


def build_server(intel: SourceIntel, name: str = "zephyr-source-intel") -> MCPServer:
    srv = MCPServer(name, instructions=INSTRUCTIONS, version="0.1.0")

    def guard(fn, *args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface the cause to the model, do not crash the server
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    @srv.tool()
    def pr_overview() -> OverviewOut:
        """Commits, changed files with +/- counts, and the MAINTAINERS areas each file falls in. Call this first."""
        return guard(lambda: _both(intel.overview(), intel.overview_data()))

    @srv.tool(structured_output=False)
    def commit_messages() -> str:
        """Full commit messages of the pull request, oldest first (Zephyr reviews the message, not only the code)."""
        return guard(intel.commit_messages)

    @srv.tool(structured_output=False)
    def file_diff(path: str, context_lines: int = 3) -> str:
        """Unified diff of one changed file between base and head, with the given context."""
        return guard(intel.diff_for, path, context_lines)

    @srv.tool(structured_output=False)
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a file at the head commit with line numbers; lines added by the PR are marked '+'.
        At most 400 lines per call; omit end_line to read 400 lines from start_line."""
        return guard(intel.read, path, start_line, end_line)

    @srv.tool(structured_output=False)
    def read_file_at_base(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a file as it was at the base commit, to compare old and new behavior."""
        return guard(intel.read_base, path, start_line, end_line)

    @srv.tool(structured_output=False)
    def search(
        pattern: str,
        path: str | None = None,
        regex: bool = True,
        whole_word: bool = False,
        ignore_case: bool = False,
        max_results: int = 100,
    ) -> str:
        """grep the head tree. path may be a directory (drivers/i2c) or a glob (drivers/**/*.c).
        Regex is POSIX extended as in git grep: use [[:space:]] not \\s, and whole_word instead of \\b."""
        return guard(
            intel.search,
            pattern,
            path,
            regex=regex,
            whole_word=whole_word,
            ignore_case=ignore_case,
            max_results=max_results,
        )

    @srv.tool(structured_output=False)
    def list_files(pattern: str) -> str:
        """List files matching a glob at the head commit, e.g. dts/bindings/i2c/*.yaml or tests/drivers/i2c/**."""
        return guard(intel.list_files, pattern)

    @srv.tool()
    def find_definition(name: str, path: str | None = None) -> DefinitionsOut:
        """Where a C function, macro, struct, typedef, Kconfig symbol (CONFIG_FOO), devicetree compatible
        (vendor,device) or CMake function is defined. Optionally restrict to a path."""
        return guard(lambda: _both(intel.definition(name, path), intel.definition_data(name, path)))

    @srv.tool(structured_output=False)
    def find_references(name: str, path: str | None = None, max_results: int = 60) -> str:
        """Who uses a symbol: counts per area, then individual sites, changed files first."""
        return guard(intel.references, name, path, max_results)

    @srv.tool(structured_output=False)
    def kconfig_symbol(name: str) -> str:
        """Every definition block of a Kconfig symbol (depends on, select, default, help) and its users
        in Kconfig, .conf/defconfig, CMake and source."""
        return guard(intel.kconfig, name)

    @srv.tool(structured_output=False)
    def dt_binding(compatible_or_name: str) -> str:
        """The devicetree binding YAML for a compatible, plus the drivers (DT_DRV_COMPAT) and nodes using it."""
        return guard(intel.dt_binding, compatible_or_name)

    @srv.tool()
    def maintainers(path: str) -> MaintainersOut:
        """MAINTAINERS.yml areas covering a path: status, maintainers, collaborators, labels, test identifiers."""
        return guard(lambda: _both(intel.maintainers_for(path), intel.maintainers_data(path)))

    @srv.tool(structured_output=False)
    def related_tests(path_or_symbol: str) -> str:
        """Twister suites (tests.yaml / sample.yaml) covering a path or mentioning a symbol, with tags and platforms."""
        return guard(intel.related_tests, path_or_symbol)

    @srv.tool(structured_output=False)
    def test_candidates() -> str:
        """Everything a test plan for this change should consider: suites the PR edits, MAINTAINERS test
        identifiers, implicated twister tags, suites covering the changed source (with scenarios, tags,
        integration_platforms, platform_allow), and the platforms implicated by changed boards, SoCs,
        architectures and driver bindings."""
        return guard(intel.test_candidates)

    @srv.tool()
    def list_platforms(fragment: str) -> PlatformsOut:
        """Valid twister platform identifiers (board/soc[/qualifiers]) whose name, board or vendor contains
        the fragment, e.g. 'nrf5340', 'qemu', 'native', 'stm32h7'. Use it to validate a test plan."""
        return guard(lambda: _both(intel.platforms(fragment), intel.platforms_data(fragment)))

    @srv.tool(structured_output=False)
    def file_history(path: str, count: int = 10) -> str:
        """Recent commits touching a path at the head commit."""
        return guard(intel.history, path, count)

    @srv.tool(structured_output=False)
    def blame(path: str, start_line: int, end_line: int) -> str:
        """git blame for a line range at the head commit (at most 200 lines)."""
        return guard(intel.blame, path, start_line, end_line)

    @srv.tool(structured_output=False)
    def project_guidelines(topic: str = "agents") -> str:
        """Zephyr contribution rules. Topics: agents, guidelines, expectations, reviewer, style-c, style-kconfig,
        style-cmake, style-devicetree, coding-guidelines, bindings, kconfig-tips, or a doc/ path."""
        return guard(intel.guidelines, topic)

    return srv


def add_server_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=".", help="Path to the Zephyr checkout (default: cwd)")
    p.add_argument("--head", default="HEAD", help="Commit under review (default: HEAD)")
    p.add_argument("--base", required=True, help="Base commit or ref, e.g. upstream/main or a merge-base SHA")
    p.add_argument("--max-chars", type=int, default=24_000, help="Per-result size cap")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)


def _only_broken_pipes(exc: BaseException) -> bool:
    """True when an exception (group) consists solely of BrokenPipeError: the client went away."""
    if isinstance(exc, BrokenPipeError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_only_broken_pipes(e) for e in exc.exceptions)
    return False


def serve(args: argparse.Namespace) -> int:
    intel = SourceIntel(args.repo, args.head, args.base, max_chars=args.max_chars)
    srv = build_server(intel)
    try:
        if args.transport == "stdio":
            srv.run(transport="stdio")
        else:
            srv.run(transport="streamable-http", host=args.host, port=args.port)
    except BaseException as exc:  # noqa: BLE001 - classify, then re-raise anything real
        if _only_broken_pipes(exc):
            # The orchestrator closed the pipe (it finished or failed). Nothing to report here, and
            # printing a traceback would only hide the orchestrator's own message.
            try:
                sys.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            return 0
        raise
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m zephyr_reviewer.mcp_server")
    add_server_args(p)
    return serve(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
