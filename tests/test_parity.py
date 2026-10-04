"""CLI and MCP parity: both fronts emit the core's record unchanged.

For each of the eight operations and each recorded fixture, the CLI's `--json`
output (its `main` run in-process with the fixture runner, stdout captured)
parses to exactly the MCP tool's structured content for the same arguments.
The comparison is plain equality of the two parsed objects; a difference is a
defect in a front. Every test name carries `parity`.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent
from test_journal import SECRETS_UNIT

from journalmcp.cli import main
from journalmcp.runner import FixtureRunner
from journalmcp.server import build_server

pytestmark = pytest.mark.anyio

CRON, GCR = "cron.service", "gcr-ssh-agent.service"

# (CLI argv without --json, MCP tool, MCP arguments). One row per recorded
# fixture shape; the rows that leave `lines` out on both sides prove the two
# fronts share the core's defaults.
CASES: list[tuple[list[str], str, dict[str, Any]]] = [
    (["units"], "list_units", {"scope": "user"}),
    (["units", "--scope", "system"], "list_units", {"scope": "system"}),
    (["units", "--state", "failed"], "list_units", {"scope": "user", "state": "failed"}),
    (["units", "--scope", "system", "--state", "failed"], "list_units", {"scope": "system", "state": "failed"}),
    (["failed"], "failed_units", {"scope": "user"}),
    (["failed", "--scope", "system"], "failed_units", {"scope": "system"}),
    (["timers"], "list_timers", {"scope": "user"}),
    (["timers", "--scope", "system"], "list_timers", {"scope": "system"}),
    (["status", "--unit", GCR], "unit_status", {"unit": GCR, "scope": "user"}),
    (["status", "--unit", CRON, "--scope", "system"], "unit_status", {"unit": CRON, "scope": "system"}),
    (["status", "--unit", CRON, "--scope", "system", "--lines", "50"], "unit_status",
     {"unit": CRON, "scope": "system", "lines": 50}),
    (["status", "--unit", SECRETS_UNIT, "--scope", "system"], "unit_status",
     {"unit": SECRETS_UNIT, "scope": "system"}),
    (["journal", "--unit", GCR, "--lines", "20"], "journal", {"scope": "user", "unit": GCR, "lines": 20}),
    (["journal", "--unit", GCR, "--lines", "50"], "journal", {"scope": "user", "unit": GCR, "lines": 50}),
    (["journal", "--unit", CRON, "--scope", "system", "--lines", "20"], "journal",
     {"scope": "system", "unit": CRON, "lines": 20}),
    (["journal", "--unit", CRON, "--scope", "system", "--lines", "50"], "journal",
     {"scope": "system", "unit": CRON, "lines": 50}),
    (["journal", "--unit", SECRETS_UNIT, "--scope", "system", "--lines", "20"], "journal",
     {"scope": "system", "unit": SECRETS_UNIT, "lines": 20}),
    (["journal", "--unit", "synthetic-overflow.service", "--scope", "system", "--lines", "5"], "journal",
     {"scope": "system", "unit": "synthetic-overflow.service", "lines": 5}),
    (["journal", "--unit", "synthetic-oversized.service", "--scope", "system", "--lines", "10"], "journal",
     {"scope": "system", "unit": "synthetic-oversized.service", "lines": 10}),
    (["unit-file", "--unit", GCR], "unit_file", {"unit": GCR, "scope": "user"}),
    (["unit-file", "--unit", CRON, "--scope", "system"], "unit_file", {"unit": CRON, "scope": "system"}),
    (["unit-file", "--unit", SECRETS_UNIT, "--scope", "system"], "unit_file",
     {"unit": SECRETS_UNIT, "scope": "system"}),
    (["boots"], "boots", {}),
    (["summary"], "system_summary", {}),
]

SPEC_TOOLS = {
    "list_units", "failed_units", "list_timers", "unit_status",
    "journal", "boots", "unit_file", "system_summary",
}


def cli_json(argv: list[str], runner: FixtureRunner) -> Any:
    out, err = io.StringIO(), io.StringIO()
    code = main([*argv, "--json"], runner=runner, stdout=out, stderr=err)
    assert (code, err.getvalue()) == (0, ""), err.getvalue()
    return json.loads(out.getvalue())


def cli_error(argv: list[str], runner: FixtureRunner) -> str:
    out, err = io.StringIO(), io.StringIO()
    code = main([*argv, "--json"], runner=runner, stdout=out, stderr=err)
    assert (code, out.getvalue()) == (1, "")
    prefix = "journalmcp: "
    text = err.getvalue()
    assert text.startswith(prefix) and text.endswith("\n")
    return text[len(prefix):-1]


def test_parity_cases_cover_all_eight_operations() -> None:
    assert {tool for _, tool, _ in CASES} == SPEC_TOOLS


@pytest.mark.parametrize(("argv", "tool", "arguments"), CASES, ids=[" ".join(c[0]) for c in CASES])
async def test_parity_cli_json_equals_mcp_structured_content(
    fixture_runner: FixtureRunner, argv: list[str], tool: str, arguments: dict[str, Any]
) -> None:
    from_cli = cli_json(argv, fixture_runner)
    async with Client(build_server(fixture_runner)) as client:
        result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert from_cli == result.structured_content


@pytest.mark.parametrize(("argv", "tool", "arguments"), CASES, ids=[" ".join(c[0]) for c in CASES])
async def test_parity_mcp_text_content_is_the_same_object(
    fixture_runner: FixtureRunner, argv: list[str], tool: str, arguments: dict[str, Any]
) -> None:
    # The SDK also puts the result in `content` as JSON text for the model;
    # it must be the same record, not a second rendering of it.
    from_cli = cli_json(argv, fixture_runner)
    async with Client(build_server(fixture_runner)) as client:
        result = await client.call_tool(tool, arguments)
    (block,) = result.content
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == from_cli


ERROR_CASES: list[tuple[list[str], str, dict[str, Any]]] = [
    (["status", "--unit=-x.service"], "unit_status", {"unit": "-x.service", "scope": "user"}),
    (["unit-file", "--unit", "a@b@c.service"], "unit_file", {"unit": "a@b@c.service", "scope": "user"}),
    (["journal", "--unit", CRON, "--lines", "0"], "journal", {"scope": "user", "unit": CRON, "lines": 0}),
    (["units", "--state=-x"], "list_units", {"scope": "user", "state": "-x"}),
    (["journal", "--since", "bad;value"], "journal", {"scope": "user", "since": "bad;value"}),
]


@pytest.mark.parametrize(("argv", "tool", "arguments"), ERROR_CASES, ids=[" ".join(c[0]) for c in ERROR_CASES])
async def test_parity_a_core_error_carries_the_same_message_in_both_fronts(
    fixture_runner: FixtureRunner, argv: list[str], tool: str, arguments: dict[str, Any]
) -> None:
    message = cli_error(argv, fixture_runner)
    async with Client(build_server(fixture_runner)) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    (block,) = result.content
    assert isinstance(block, TextContent)
    assert block.text == f"Error executing tool {tool}: {message}"
