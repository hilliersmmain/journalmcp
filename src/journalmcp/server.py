"""The MCP front: the eight core operations as MCP tools over stdio. No other logic.

Each tool is one call into the core and returns the core's result dataclass,
which the SDK turns into the call's structured content: the same object the
CLI prints under `--json`. A core error is re-raised as the SDK's `ToolError`,
so the client reads the core's message (any other exception reaches the client
only as "Error executing tool <name>"). The one transport is stdio: this
module never opens a listening socket and never reaches the network.

This is the one module in the package that imports third-party code: the
`mcp` SDK and `pydantic`, the SDK's own validation library (for the strict
`lines` type); the CLI imports this module lazily, inside `serve`, so every
other subcommand still runs on the standard library alone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import Strict

from journalmcp import __version__, core
from journalmcp.errors import JournalmcpError
from journalmcp.runner import Runner

NAME = "journalmcp"

# Every tool reads and none reaches past the local systemd managers.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# `lines` is an integer and nothing else. The SDK validates arguments with
# pydantic in lax mode, which would turn true into 1 and "20" or 20.0 into 20
# before the core sees them; the core (and the CLI) reject those, so the MCP
# front must too. Strict keeps the schema `integer` and refuses the coercions.
Lines = Annotated[int, Strict()]

INSTRUCTIONS = (
    "Read-only queries over systemd and the journal on this machine. No tool changes "
    "any unit. scope is 'user' (the user's manager) or 'system'. Journal text is "
    "redacted and capped; a cut is marked [TRUNCATED] and sets truncated to true."
)


def _call[T](operation: Callable[[], T]) -> T:
    """Run one core call; a core error becomes a tool error the client can read."""
    try:
        return operation()
    except JournalmcpError as exc:
        raise ToolError(str(exc)) from exc


def build_server(runner: Runner | None = None) -> MCPServer:
    """The server with its eight tools and one prompt, over the given runner.

    `None` means the core's real subprocess runner; tests pass a fixture runner.
    """
    mcp = MCPServer(NAME, version=__version__, instructions=INSTRUCTIONS, log_level="WARNING")

    @mcp.tool(annotations=READ_ONLY)
    def list_units(
        scope: str, state: str | None = None, type: str | None = None, pattern: str | None = None
    ) -> core.UnitsResult:
        """List the units systemd has in memory (systemctl list-units --all). Read-only.

        scope is 'user' or 'system'. state filters on a LOAD, ACTIVE or SUB state,
        type on a unit type such as 'service', pattern on a glob over unit names.
        """
        return _call(lambda: core.list_units(scope, state, type, pattern, runner=runner))

    @mcp.tool(annotations=READ_ONLY)
    def failed_units(scope: str) -> core.UnitsResult:
        """List the failed units of one manager. Read-only. scope is 'user' or 'system'."""
        return _call(lambda: core.failed_units(scope, runner=runner))

    @mcp.tool(annotations=READ_ONLY)
    def list_timers(scope: str) -> core.TimersResult:
        """List the timers of one manager, with next and last run times in
        microseconds since the epoch. Read-only. scope is 'user' or 'system'."""
        return _call(lambda: core.list_timers(scope, runner=runner))

    @mcp.tool(annotations=READ_ONLY)
    def unit_status(unit: str, scope: str, lines: Lines = core.DEFAULT_STATUS_LINES) -> core.UnitStatusResult:
        """One unit's load, active and sub state, result and main PID, plus its last
        journal entries (default 20, at most 1000). Read-only.

        Text is redacted and capped at 4096 bytes per value and 65536 bytes in all;
        a cut value ends in [TRUNCATED] and sets truncated to true.
        """
        return _call(lambda: core.unit_status(unit, scope, lines, runner=runner))

    @mcp.tool(annotations=READ_ONLY)
    def journal(
        scope: str,
        unit: str | None = None,
        since: str | None = None,
        until: str | None = None,
        priority: str | None = None,
        grep: str | None = None,
        lines: Lines = core.DEFAULT_JOURNAL_LINES,
    ) -> core.JournalResult:
        """Journal entries, oldest first, reduced to an allowlist of fields. Read-only.

        Filters: unit, since and until (such as '2026-10-03 12:00' or '-1h'),
        priority (such as 'err' or 'warning..crit'), grep (a regular expression on
        MESSAGE), lines (default 100, at most 1000). Secrets are redacted; text is
        capped at 4096 bytes per value and 65536 bytes in all, and a cut ends in
        [TRUNCATED] and sets truncated to true.
        """
        return _call(
            lambda: core.journal(scope, unit, since, until, priority, grep, lines, runner=runner)
        )

    @mcp.tool(annotations=READ_ONLY)
    def boots() -> core.BootsResult:
        """The boots the journal records; index 0 is the current boot. Read-only."""
        return _call(lambda: core.boots(runner=runner))

    @mcp.tool(annotations=READ_ONLY)
    def unit_file(unit: str, scope: str) -> core.UnitFileResult:
        """The unit file text as systemctl cat prints it. Read-only.

        Redacted, and capped at 65536 bytes; a cut ends in [TRUNCATED] and sets
        truncated to true.
        """
        return _call(lambda: core.unit_file(unit, scope, runner=runner))

    @mcp.tool(annotations=READ_ONLY)
    def system_summary() -> core.SystemSummary:
        """The system manager's state word, failed-unit and timer counts for both
        managers, and the current boot's first-entry time. Read-only."""
        return _call(lambda: core.system_summary(runner=runner))

    @mcp.prompt(
        title="Triage a failed unit",
        description="Find out why one systemd unit failed, using the read-only tools.",
    )
    def triage_failed_unit(unit: str, scope: str) -> str:
        """The three read-only calls to make for one unit, in order."""
        # The unit and scope are checked by the core's own validators, so the
        # message never carries text the tools would refuse. A failure is a
        # protocol error with the core's message (any other exception reaches
        # the client only as an internal error).
        try:
            name = core.validate_unit_name(unit)
            valid_scope = core.validate_scope(scope)
        except JournalmcpError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from exc
        return (
            f"The systemd unit {name} (scope {valid_scope}) may have failed. Investigate it "
            "with the journalmcp tools, in this order:\n"
            f"1. Call unit_status with unit={name!r} and scope={valid_scope!r} for its "
            "states, result and last entries.\n"
            f"2. Call journal with scope={valid_scope!r} and unit={name!r} for what it "
            "logged before and after it stopped.\n"
            f"3. Call unit_file with unit={name!r} and scope={valid_scope!r} to read how it "
            "is configured.\n"
            "Then report the likely cause, quoting the journal lines and unit-file settings "
            "that support it. Every tool is read-only: suggest a fix, do not claim to apply one."
        )

    return mcp


def main() -> None:
    """Serve the tools over stdio with the real runner until the client closes stdin."""
    build_server().run(transport="stdio")
