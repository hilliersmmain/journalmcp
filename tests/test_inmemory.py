"""The MCP front through the SDK's in-memory client: no subprocess, no transport.

Every test name carries `inmemory`, so `pytest -k inmemory` selects them all.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from conftest import fixture_rows
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from test_journal import INVENTED_SECRETS, SECRETS_UNIT

from journalmcp import core
from journalmcp.runner import FixtureRunner, RunResult
from journalmcp.server import build_server

pytestmark = pytest.mark.anyio

# Spec section 3.2, written out in test_parity rather than read from the server.
from test_parity import SPEC_TOOLS

CRON, GCR = "cron.service", "gcr-ssh-agent.service"


class RecordingRunner:
    """A fixture runner that also records every argv it is asked to run."""

    def __init__(self, inner: FixtureRunner) -> None:
        self.inner = inner
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout: float) -> RunResult:
        self.calls.append(argv)
        return self.inner.run(argv, timeout)


async def call(runner: object, name: str, arguments: dict[str, Any]) -> CallToolResult:
    async with Client(build_server(runner)) as client:  # type: ignore[arg-type]
        return await client.call_tool(name, arguments)


def ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    assert result.structured_content is None
    (block,) = result.content
    assert isinstance(block, TextContent)
    return block.text


# --- listing ---------------------------------------------------------------------


async def test_inmemory_list_tools_names_exactly_the_eight_spec_tools(fixture_runner: FixtureRunner) -> None:
    async with Client(build_server(fixture_runner)) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == SPEC_TOOLS


async def test_inmemory_every_tool_is_annotated_read_only_and_closed_world(fixture_runner: FixtureRunner) -> None:
    async with Client(build_server(fixture_runner)) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is False, tool.name


async def test_inmemory_every_tool_has_an_output_schema_and_a_read_only_description(
    fixture_runner: FixtureRunner,
) -> None:
    async with Client(build_server(fixture_runner)) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.output_schema is not None, tool.name
        assert tool.description is not None and "Read-only" in tool.description, tool.name


async def test_inmemory_tool_parameters_and_defaults_follow_the_spec(fixture_runner: FixtureRunner) -> None:
    async with Client(build_server(fixture_runner)) as client:
        schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
    expected = {
        "list_units": (["scope"], ["scope", "state", "type", "pattern"]),
        "failed_units": (["scope"], ["scope"]),
        "list_timers": (["scope"], ["scope"]),
        "unit_status": (["unit", "scope"], ["unit", "scope", "lines"]),
        "journal": (["scope"], ["scope", "unit", "since", "until", "priority", "grep", "lines"]),
        "boots": ([], []),
        "unit_file": (["unit", "scope"], ["unit", "scope"]),
        "system_summary": ([], []),
    }
    for name, (required, params) in expected.items():
        schema = schemas[name]
        assert sorted(schema.get("required", [])) == sorted(required), name
        assert list(schema.get("properties", {})) == params, name
    assert schemas["unit_status"]["properties"]["lines"]["default"] == 20
    assert schemas["journal"]["properties"]["lines"]["default"] == 100


async def test_inmemory_list_prompts_names_triage_failed_unit_alone(fixture_runner: FixtureRunner) -> None:
    async with Client(build_server(fixture_runner)) as client:
        prompts = (await client.list_prompts()).prompts
    assert [p.name for p in prompts] == ["triage_failed_unit"]
    assert sorted((a.name, a.required) for a in prompts[0].arguments or []) == [
        ("scope", True),
        ("unit", True),
    ]


# --- every tool once, against the fixtures -----------------------------------------


@pytest.mark.parametrize("scope", ["user", "system"])
async def test_inmemory_list_units_returns_the_recorded_units(fixture_runner: FixtureRunner, scope: str) -> None:
    content = ok(await call(fixture_runner, "list_units", {"scope": scope}))
    assert content == core.list_units(scope, runner=fixture_runner).to_dict()
    assert [u["unit"] for u in content["units"]] == [
        r["unit"] for r in fixture_rows(core.list_units_argv(scope))
    ]
    assert content["scope"] == scope


@pytest.mark.parametrize("scope", ["user", "system"])
async def test_inmemory_failed_units_returns_the_recorded_failures(fixture_runner: FixtureRunner, scope: str) -> None:
    content = ok(await call(fixture_runner, "failed_units", {"scope": scope}))
    assert content == core.failed_units(scope, runner=fixture_runner).to_dict()
    assert [u["unit"] for u in content["units"]] == [
        r["unit"] for r in fixture_rows(core.failed_units_argv(scope))
    ]


@pytest.mark.parametrize("scope", ["user", "system"])
async def test_inmemory_list_timers_returns_the_recorded_timers(fixture_runner: FixtureRunner, scope: str) -> None:
    content = ok(await call(fixture_runner, "list_timers", {"scope": scope}))
    assert content == core.list_timers(scope, runner=fixture_runner).to_dict()
    assert [t["unit"] for t in content["timers"]] == [
        r["unit"] for r in fixture_rows(core.list_timers_argv(scope))
    ]
    assert content["timers"]


async def test_inmemory_unit_status_returns_states_and_entries(fixture_runner: FixtureRunner) -> None:
    content = ok(await call(fixture_runner, "unit_status", {"unit": CRON, "scope": "system"}))
    assert content == core.unit_status(CRON, "system", runner=fixture_runner).to_dict()
    assert (content["unit"], content["scope"], content["load_state"]) == (CRON, "system", "loaded")
    assert 0 < len(content["entries"]) <= 20


async def test_inmemory_journal_returns_allowlisted_entries(fixture_runner: FixtureRunner) -> None:
    content = ok(await call(fixture_runner, "journal", {"scope": "user", "unit": GCR, "lines": 50}))
    assert content == core.journal("user", unit=GCR, lines=50, runner=fixture_runner).to_dict()
    assert content["entries"]
    for entry in content["entries"]:
        assert set(entry) <= set(core.JOURNAL_FIELDS)


async def test_inmemory_boots_returns_the_recorded_boots(fixture_runner: FixtureRunner) -> None:
    content = ok(await call(fixture_runner, "boots", {}))
    assert content == core.boots(runner=fixture_runner).to_dict()
    assert [b["boot_id"] for b in content["boots"]] == [
        r["boot_id"] for r in fixture_rows(core.boots_argv())
    ]


async def test_inmemory_unit_file_returns_the_recorded_text(fixture_runner: FixtureRunner) -> None:
    content = ok(await call(fixture_runner, "unit_file", {"unit": GCR, "scope": "user"}))
    assert content == core.unit_file(GCR, "user", runner=fixture_runner).to_dict()
    assert content["text"].startswith("# /")
    assert content["truncated"] is False


async def test_inmemory_system_summary_returns_the_counts(fixture_runner: FixtureRunner) -> None:
    content = ok(await call(fixture_runner, "system_summary", {}))
    assert content == core.system_summary(runner=fixture_runner).to_dict()
    assert content["state"] in core.SYSTEM_STATES
    assert content["timers_system"] == len(fixture_rows(core.list_timers_argv("system")))


# --- redaction and caps hold in this front too --------------------------------------


async def test_inmemory_journal_is_redacted(fixture_runner: FixtureRunner) -> None:
    result = await call(fixture_runner, "journal", {"scope": "system", "unit": SECRETS_UNIT, "lines": 20})
    content = ok(result)
    messages = [e["MESSAGE"] for e in content["entries"]]
    assert messages[1] == "Authorization: Bearer [REDACTED:bearer] accepted"
    assert messages[2] == "login ok password=[REDACTED:password] secret=[REDACTED:secret]"
    assert "SRC=192.0.2.7 DST=198.51.100.3" in messages[4]
    # Neither channel carries a secret: not the structured content, not the text.
    emitted = json.dumps(content) + "".join(
        b.text for b in result.content if isinstance(b, TextContent)
    )
    for secret in INVENTED_SECRETS:
        assert secret not in emitted, secret


async def test_inmemory_unit_file_is_redacted(fixture_runner: FixtureRunner) -> None:
    content = ok(await call(fixture_runner, "unit_file", {"unit": SECRETS_UNIT, "scope": "system"}))
    assert "Environment=SECRET=[REDACTED:secret]" in content["text"]
    for secret in INVENTED_SECRETS:
        assert secret not in content["text"], secret


async def test_inmemory_journal_over_the_line_cap_carries_the_marker(fixture_runner: FixtureRunner) -> None:
    content = ok(
        await call(fixture_runner, "journal", {"scope": "system", "unit": "synthetic-overflow.service", "lines": 5})
    )
    assert [e["MESSAGE"] for e in content["entries"]] == [f"entry {i}" for i in range(3, 8)]
    assert content["truncated"] is True


async def test_inmemory_journal_over_the_byte_cap_carries_the_marker(fixture_runner: FixtureRunner) -> None:
    content = ok(
        await call(fixture_runner, "journal", {"scope": "system", "unit": "synthetic-oversized.service", "lines": 10})
    )
    first = content["entries"][0]["MESSAGE"]
    assert first.endswith(core.TRUNCATED_MARKER)
    assert len(first.encode("utf-8")) <= core.VALUE_BYTES_MAX
    assert content["truncated"] is True


# --- errors come back as tool errors the client can read ------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_units", {"scope": "bogus"}),
        ("failed_units", {"scope": "User"}),
        ("list_timers", {"scope": ""}),
        ("unit_status", {"unit": CRON, "scope": "system "}),
        ("journal", {"scope": "both"}),
        ("unit_file", {"unit": CRON, "scope": "root"}),
    ],
)
async def test_inmemory_a_bad_scope_is_a_readable_tool_error(
    fixture_runner: FixtureRunner, tool: str, arguments: dict[str, Any]
) -> None:
    text = error_text(await call(fixture_runner, tool, arguments))
    assert text == f"Error executing tool {tool}: scope must be 'user' or 'system', not {arguments['scope']!r}"


@pytest.mark.parametrize("value", [True, 20.0, "20"], ids=["bool", "float", "str"])
@pytest.mark.parametrize("tool", ["journal", "unit_status"])
async def test_inmemory_lines_that_is_not_an_integer_is_refused_like_the_core(
    fixture_runner: FixtureRunner, tool: str, value: object
) -> None:
    # The core rejects True, "20" and 20.0 as lines; the SDK's lax coercion must
    # not turn them into 1 and 20 before the core sees them.
    arguments: dict[str, Any] = {"scope": "system", "lines": value}
    if tool == "unit_status":
        arguments["unit"] = CRON
    text = error_text(await call(fixture_runner, tool, arguments))
    assert "lines" in text and "integer" in text, text


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("unit_status", {"unit": "not a unit", "scope": "user"}),
        ("unit_file", {"unit": "cron", "scope": "system"}),
        ("journal", {"scope": "user", "unit": "a@b@c.service"}),
    ],
)
async def test_inmemory_a_bad_unit_name_is_a_readable_tool_error(
    fixture_runner: FixtureRunner, tool: str, arguments: dict[str, Any]
) -> None:
    text = error_text(await call(fixture_runner, tool, arguments))
    assert text.startswith(f"Error executing tool {tool}: unit name")


async def test_inmemory_a_failed_command_is_a_readable_tool_error() -> None:
    class Failing:
        def run(self, argv: list[str], timeout: float) -> RunResult:
            return RunResult(1, "", "No files found for cron.service.\n")

    text = error_text(await call(Failing(), "unit_file", {"unit": CRON, "scope": "system"}))
    assert text == "Error executing tool unit_file: systemctl exited 1: No files found for cron.service."


async def test_inmemory_a_timeout_is_a_readable_tool_error() -> None:
    class Slow:
        def run(self, argv: list[str], timeout: float) -> RunResult:
            raise subprocess.TimeoutExpired(argv, timeout)

    text = error_text(await call(Slow(), "boots", {}))
    assert text == "Error executing tool boots: journalctl timed out after 10 s"


async def test_inmemory_the_server_keeps_serving_after_a_tool_error(fixture_runner: FixtureRunner) -> None:
    async with Client(build_server(fixture_runner)) as client:
        bad = await client.call_tool("list_units", {"scope": "bogus"})
        good = await client.call_tool("list_units", {"scope": "user"})
    assert bad.is_error
    assert not good.is_error


# --- injection through call_tool ----------------------------------------------------


@pytest.mark.parametrize("unit", ["--version", "-x.service", "--help.service"])
async def test_inmemory_a_unit_beginning_with_a_dash_is_refused_before_any_argv(
    fixture_runner: FixtureRunner, unit: str
) -> None:
    runner = RecordingRunner(fixture_runner)
    for tool, arguments in [
        ("unit_status", {"unit": unit, "scope": "user"}),
        ("unit_file", {"unit": unit, "scope": "user"}),
        ("journal", {"scope": "user", "unit": unit}),
    ]:
        text = error_text(await call(runner, tool, arguments))
        assert text.startswith(f"Error executing tool {tool}: unit name"), text
    # The core's validator refused it: nothing ran, so no argv was changed.
    assert runner.calls == []


# --- the prompt ---------------------------------------------------------------------


async def test_inmemory_the_triage_prompt_names_the_three_tools_in_order(fixture_runner: FixtureRunner) -> None:
    async with Client(build_server(fixture_runner)) as client:
        result = await client.get_prompt("triage_failed_unit", {"unit": CRON, "scope": "system"})
    (message,) = result.messages
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    text = message.content.text
    positions = [text.index(f"Call {tool} ") for tool in ("unit_status", "journal", "unit_file")]
    assert positions == sorted(positions)
    assert CRON in text and "'system'" in text
    assert "likely cause" in text


async def test_inmemory_the_triage_prompt_refuses_a_bad_unit_with_the_core_message(
    fixture_runner: FixtureRunner,
) -> None:
    async with Client(build_server(fixture_runner)) as client:
        with pytest.raises(MCPError) as caught:
            await client.get_prompt("triage_failed_unit", {"unit": "-x.service", "scope": "user"})
        with pytest.raises(MCPError) as caught_scope:
            await client.get_prompt("triage_failed_unit", {"unit": CRON, "scope": "everything"})
    assert caught.value.message.startswith("unit name may not begin with -")
    assert caught_scope.value.message.startswith("scope must be")
