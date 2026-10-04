"""The CLI front: parse arguments, call the core, render. No other logic.

Everything this module imports at load time is the standard library or the
core, so `./journalmcp --version` works under a bare `PATH` with no virtual
environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from journalmcp import __version__, core
from journalmcp.errors import JournalmcpError
from journalmcp.runner import Runner

PROG = "journalmcp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Read-only queries over systemd. Changes nothing.",
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROG} {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help_text: str, *, scoped: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        if scoped:
            p.add_argument(
                "--scope",
                choices=core.SCOPES,
                default="user",
                help="the user manager (default) or the system manager",
            )
        p.add_argument(
            "--json",
            action="store_true",
            help="print the result object as JSON instead of a table",
        )
        return p

    def unit_flag(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument("--unit", required=required, help="a unit name, such as cron.service")

    units = add("units", "List units systemd has in memory.")
    units.add_argument("--state", help="only units in this LOAD, ACTIVE or SUB state")
    units.add_argument("--type", help="only units of this type, such as service")
    units.add_argument("--pattern", help="only units whose name matches this glob")
    add("failed", "List failed units.")
    add("timers", "List timers and when they fire.")

    status = add("status", "One unit's state and its last journal entries.")
    unit_flag(status, required=True)
    status.add_argument(
        "--lines", type=int, default=core.DEFAULT_STATUS_LINES,
        help=f"journal entries to show (default {core.DEFAULT_STATUS_LINES})",
    )
    unit_file = add("unit-file", "The unit file as systemctl cat prints it.")
    unit_flag(unit_file, required=True)

    journal = add("journal", "Journal entries, newest last, with the secrets redacted.")
    unit_flag(journal, required=False)
    journal.add_argument(
        "--since",
        help="not older than this, such as '2026-10-03 12:00' or --since=-1h "
        "(a value that begins with - needs the = form)",
    )
    journal.add_argument("--until", help="not newer than this")
    journal.add_argument("--priority", help="a syslog level such as err, or a range such as err..crit")
    journal.add_argument("--grep", help="only entries whose MESSAGE matches this regular expression")
    journal.add_argument(
        "--lines", type=int, default=core.DEFAULT_JOURNAL_LINES,
        help=f"entries to show (default {core.DEFAULT_JOURNAL_LINES})",
    )
    # No --scope on these two: boots belong to the machine, and the summary
    # covers both managers.
    add("boots", "The recorded boots, current boot first.", scoped=False)
    add("summary", "The manager state, failed-unit and timer counts, and the boot time.", scoped=False)
    # The MCP front: no --scope and no --json, since the client names both per call.
    sub.add_parser(
        "serve",
        help="Serve the same operations as MCP tools over stdio.",
        description="Serve the same operations as MCP tools over stdio, until stdin closes.",
    )
    return parser


def run_command(args: argparse.Namespace, runner: Runner | None) -> Any:
    if args.command == "units":
        return core.list_units(
            args.scope, args.state, args.type, args.pattern, runner=runner
        )
    if args.command == "failed":
        return core.failed_units(args.scope, runner=runner)
    if args.command == "timers":
        return core.list_timers(args.scope, runner=runner)
    if args.command == "status":
        return core.unit_status(args.unit, args.scope, args.lines, runner=runner)
    if args.command == "unit-file":
        return core.unit_file(args.unit, args.scope, runner=runner)
    if args.command == "journal":
        return core.journal(
            args.scope, args.unit, args.since, args.until, args.priority, args.grep, args.lines,
            runner=runner,
        )
    if args.command == "boots":
        return core.boots(runner=runner)
    if args.command == "summary":
        return core.system_summary(runner=runner)
    raise AssertionError(f"unhandled command {args.command!r}")


def _usec_to_text(value: int | None) -> str:
    if not value:
        return "-"
    stamp = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    return stamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]
    lines = [headers, *rows]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip()
        for line in lines
    )


def _stamp_text(entry: dict[str, str]) -> str:
    raw = entry.get("__REALTIME_TIMESTAMP", "")
    return _usec_to_text(int(raw)) if raw.isdigit() else "-"


def _journal_lines(entries: list[dict[str, str]]) -> str:
    """One line per entry: `TIME  UNIT  IDENTIFIER[PID]: MESSAGE`; a message
    with several lines keeps them."""
    lines = []
    for e in entries:
        unit = e.get("_SYSTEMD_USER_UNIT") or e.get("_SYSTEMD_UNIT") or e.get("UNIT") or "-"
        who = e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or "-"
        if "_PID" in e:
            who += f"[{e['_PID']}]"
        lines.append(f"{_stamp_text(e)}  {unit}  {who}: {e.get('MESSAGE', '')}")
    return "\n".join(lines)


def _count_line(n: int, noun: str, scope: str | None, truncated: bool) -> str:
    text = f"{n} {noun}" + (f" ({scope})" if scope else "")
    return text + (f"\n{core.TRUNCATED_MARKER}" if truncated else "")


def render_text(result: Any) -> str:
    if isinstance(result, core.UnitsResult):
        rows = [[u.unit, u.load, u.active, u.sub, u.description] for u in result.units]
        body = _table(["UNIT", "LOAD", "ACTIVE", "SUB", "DESCRIPTION"], rows)
        return f"{body}\n\n{len(rows)} units ({result.scope})"
    if isinstance(result, core.TimersResult):
        rows = [
            [t.unit, t.activates, _usec_to_text(t.next), _usec_to_text(t.last)]
            for t in result.timers
        ]
        body = _table(["TIMER", "ACTIVATES", "NEXT", "LAST"], rows)
        return f"{body}\n\n{len(rows)} timers ({result.scope})"
    if isinstance(result, core.JournalResult):
        body = _journal_lines(result.entries)
        tail = _count_line(len(result.entries), "entries", result.scope, result.truncated)
        return f"{body}\n\n{tail}" if body else tail
    if isinstance(result, core.UnitStatusResult):
        head = [
            f"Unit: {result.unit} ({result.scope})",
            f"Load: {result.load_state}",
            f"Active: {result.active_state} ({result.sub_state})",
            f"Description: {result.description}",
        ]
        if result.result is not None:
            head.append(f"Result: {result.result}")
        if result.exec_main_pid is not None:
            head.append(f"Main PID: {result.exec_main_pid}")
        body = _journal_lines(result.entries)
        tail = _count_line(len(result.entries), "entries", result.scope, result.truncated)
        return "\n".join(head) + "\n\n" + (f"{body}\n\n{tail}" if body else tail)
    if isinstance(result, core.UnitFileResult):
        text = result.text.rstrip("\n")
        return text + (f"\n{core.TRUNCATED_MARKER}" if result.truncated else "")
    if isinstance(result, core.BootsResult):
        rows = [
            [str(b.index), b.boot_id, _usec_to_text(b.first_entry), _usec_to_text(b.last_entry)]
            for b in result.boots
        ]
        body = _table(["IDX", "BOOT ID", "FIRST ENTRY", "LAST ENTRY"], rows)
        return f"{body}\n\n{len(rows)} boots"
    if isinstance(result, core.SystemSummary):
        return "\n".join(
            [
                f"State: {result.state}",
                f"Failed units: {result.failed_units_system} (system), {result.failed_units_user} (user)",
                f"Timers: {result.timers_system} (system), {result.timers_user} (user)",
                f"Boot time: {_usec_to_text(result.boot_time)}",
            ]
        )
    raise AssertionError(f"no renderer for {type(result).__name__}")


def _serve(err: TextIO) -> int:
    """Start the MCP front. The SDK is imported here and nowhere else in the CLI,
    so every other subcommand and --version run on the standard library alone."""
    try:
        from journalmcp import server
    except ImportError as exc:
        print(
            f"{PROG}: serve needs the mcp package ({exc.name}); run it from the "
            "project's locked environment, as tools/journalmcp-mcp does",
            file=err,
        )
        return 1
    server.main()
    return 0


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(err)
    try:
        result = run_command(args, runner)
    except JournalmcpError as exc:
        print(f"{PROG}: {exc}", file=err)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2), file=out)
    else:
        print(render_text(result), file=out)
    return 0
