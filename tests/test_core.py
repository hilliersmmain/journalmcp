"""The core: scope, read verbs, argv shapes, injection, errors, and parsing."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from conftest import StubRunner, fixture_rows

from journalmcp import core
from journalmcp.errors import (
    CommandFailed,
    CommandTimeout,
    FixtureMissing,
    InvalidArgument,
    MalformedOutput,
)
from journalmcp.runner import FixtureRunner, RunResult

BASE = ["--output=json", "--all", "--no-pager"]


# --- scope -----------------------------------------------------------------


@pytest.mark.parametrize("scope", ["user", "system"])
def test_scope_accepts_the_two_literals(scope: str) -> None:
    assert core.validate_scope(scope) == scope


@pytest.mark.parametrize("scope", ["User", "", "system ", None])
def test_scope_rejects_everything_else_before_any_argv(scope: object) -> None:
    stub = StubRunner(RunResult(0, "[]", ""))
    with pytest.raises(InvalidArgument):
        core.list_units(scope, runner=stub)
    with pytest.raises(InvalidArgument):
        core.list_timers(scope, runner=stub)
    assert stub.calls == []


def test_user_scope_adds_user_and_system_adds_nothing() -> None:
    assert core.list_timers_argv("user") == ["systemctl", "--user", "list-timers", *BASE]
    assert core.list_timers_argv("system") == ["systemctl", "list-timers", *BASE]


# --- argv shapes and read verbs --------------------------------------------

# Every argv the core can build, across all eight operations and both scopes.
BUILDERS: list[tuple[str, Callable[[], list[str]]]] = [
    ("units-user", lambda: core.list_units_argv("user")),
    ("units-system", lambda: core.list_units_argv("system")),
    ("units-filtered", lambda: core.list_units_argv("user", "running", "service", "-x*")),
    ("failed-user", lambda: core.failed_units_argv("user")),
    ("failed-system", lambda: core.failed_units_argv("system")),
    ("timers-user", lambda: core.list_timers_argv("user")),
    ("timers-system", lambda: core.list_timers_argv("system")),
    ("status-user", lambda: core.unit_status_argv("x.service", "user")),
    ("status-system", lambda: core.unit_status_argv("x.service", "system")),
    ("unit-file-user", lambda: core.unit_file_argv("x.service", "user")),
    ("unit-file-system", lambda: core.unit_file_argv("x.service", "system")),
    ("journal-user", lambda: core.journal_argv("user")),
    ("journal-system", lambda: core.journal_argv("system")),
    ("journal-filtered", lambda: core.journal_argv("system", "x.service", "-1h", "now", "err", "-x", 5)),
    ("boots", core.boots_argv),
    ("summary", core.system_summary_argv),
]
OPERATIONS = (
    "units", "failed", "timers", "status", "unit-file", "journal", "boots", "summary",
)


def test_the_builders_cover_all_eight_operations() -> None:
    names = {n.split("-")[0] if not n.startswith("unit-file") else "unit-file" for n, _ in BUILDERS}
    assert names == set(OPERATIONS)


@pytest.mark.parametrize("build", [b for _, b in BUILDERS], ids=[n for n, _ in BUILDERS])
def test_every_argv_starts_with_a_read_verb(build: Callable[[], list[str]]) -> None:
    argv = build()
    assert argv[0] in ("systemctl", "journalctl")
    rest = argv[1:]
    if rest[0] == "--user":
        rest = rest[1:]
    assert rest[0] in core.READ_VERBS
    if argv[0] == "systemctl":
        assert rest[0] in core.SYSTEMCTL_READ_VERBS
    else:
        assert rest[0] in core.JOURNALCTL_READ_FORMS


def test_the_read_verb_list_is_the_fixed_one() -> None:
    assert core.SYSTEMCTL_READ_VERBS == (
        "list-units", "list-timers", "show", "cat", "is-system-running",
    )
    assert core.JOURNALCTL_READ_FORMS == ("--output=json", "--list-boots")
    assert core.READ_VERBS == core.SYSTEMCTL_READ_VERBS + core.JOURNALCTL_READ_FORMS


def test_argv_shapes_are_exact() -> None:
    assert core.list_units_argv("user") == ["systemctl", "--user", "list-units", *BASE]
    assert core.failed_units_argv("system") == [
        "systemctl", "list-units", *BASE, "--state=failed",
    ]
    assert core.list_units_argv("system", "active", "timer", "cron*") == [
        "systemctl", "list-units", *BASE, "--state=active", "--type=timer", "--", "cron*",
    ]


# --- argument injection ----------------------------------------------------

HOSTILE = {
    "leading-dash": "--now",
    "shell-metacharacters": "x; rm -rf / $(id) `id` | cat > /tmp/x",
    "newline": "running\n--user",
    "nul": "running\x00--user",
}


@pytest.mark.parametrize("value", HOSTILE.values(), ids=HOSTILE.keys())
def test_state_cannot_carry_an_option_or_metacharacters(value: str) -> None:
    with pytest.raises(InvalidArgument):
        core.list_units_argv("user", state=value)


@pytest.mark.parametrize("value", HOSTILE.values(), ids=HOSTILE.keys())
def test_type_cannot_carry_an_option_or_metacharacters(value: str) -> None:
    with pytest.raises(InvalidArgument):
        core.list_units_argv("user", type=value)


@pytest.mark.parametrize("value", [HOSTILE["leading-dash"], HOSTILE["shell-metacharacters"]])
def test_pattern_is_one_token_after_the_separator(value: str) -> None:
    argv = core.list_units_argv("user", pattern=value)
    assert argv == ["systemctl", "--user", "list-units", *BASE, "--", value]
    assert argv.index("--") == len(argv) - 2


@pytest.mark.parametrize("value", [HOSTILE["newline"], HOSTILE["nul"], ""])
def test_pattern_rejects_control_characters_and_empty(value: str) -> None:
    with pytest.raises(InvalidArgument):
        core.list_units_argv("user", pattern=value)


def test_valid_selectors_pass_through() -> None:
    argv = core.list_units_argv("user", "sub-state_1.x", "service", "user@*.service")
    assert argv[-4:] == ["--state=sub-state_1.x", "--type=service", "--", "user@*.service"]


# --- errors ----------------------------------------------------------------


def test_a_timeout_becomes_a_typed_error() -> None:
    stub = StubRunner(raises=subprocess.TimeoutExpired(["systemctl"], 0.5))
    with pytest.raises(CommandTimeout, match="timed out"):
        core.list_units("user", runner=stub, timeout=0.5)


def test_a_missing_binary_becomes_a_typed_error() -> None:
    stub = StubRunner(raises=FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(CommandFailed, match="could not start"):
        core.list_timers("system", runner=stub)


def test_a_non_zero_exit_becomes_a_typed_error() -> None:
    stub = StubRunner(RunResult(1, "", "Failed to connect to bus: No medium found\nmore\n"))
    with pytest.raises(CommandFailed, match="exited 1: Failed to connect to bus"):
        core.failed_units("user", runner=stub)


@pytest.mark.parametrize("stdout", ["not json", '{"unit": "x"}', '[{"unit": 3}]', "[1]"])
def test_malformed_output_becomes_a_typed_error(stdout: str) -> None:
    with pytest.raises(MalformedOutput):
        core.list_units("user", runner=StubRunner(RunResult(0, stdout, "")))


def test_an_argv_with_no_fixture_fails_loudly(fixture_runner: FixtureRunner) -> None:
    with pytest.raises(FixtureMissing):
        core.list_units("user", state="running", runner=fixture_runner)


# --- parsing the recordings ------------------------------------------------


def _units_match(result: core.UnitsResult, rows: list[dict[str, Any]]) -> None:
    assert len(result.units) == len(rows)
    assert [vars(u) for u in result.units] == [
        {k: r[k] for k in ("unit", "load", "active", "sub", "description")} for r in rows
    ]


@pytest.mark.parametrize("scope", ["user", "system"])
def test_list_units_parses_the_recording(scope: str, fixture_runner: FixtureRunner) -> None:
    result = core.list_units(scope, runner=fixture_runner)
    rows = fixture_rows(core.list_units_argv(scope))
    assert result.scope == scope
    assert len(rows) > 50
    _units_match(result, rows)


SECRET_ROWS = json.dumps(
    [
        {
            "unit": "backup.service",
            "load": "loaded",
            "active": "active",
            "sub": "running",
            "description": "backup to s3 token=abcdefghijklmnop1234",
        }
    ]
)


@pytest.mark.parametrize("operation", [core.list_units, core.failed_units])
def test_list_units_and_failed_units_redact_the_description_redact(
    operation: Callable[..., core.UnitsResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    # unit_status redacts Description; the two listings emit the same property.
    result = operation("system", runner=StubRunner(RunResult(0, SECRET_ROWS, "")))
    assert result.units[0].description == "backup to s3 token=[REDACTED:token]"
    assert "abcdefghijklmnop1234" not in json.dumps(result.to_dict())
    monkeypatch.setenv("JOURNALMCP_REDACT", "off")
    verbatim = operation("system", runner=StubRunner(RunResult(0, SECRET_ROWS, "")))
    assert verbatim.units[0].description == "backup to s3 token=abcdefghijklmnop1234"


@pytest.mark.parametrize("scope", ["user", "system"])
def test_failed_units_parses_the_recording(scope: str, fixture_runner: FixtureRunner) -> None:
    result = core.failed_units(scope, runner=fixture_runner)
    rows = fixture_rows(core.failed_units_argv(scope))
    _units_match(result, rows)
    assert all(u.active == "failed" for u in result.units)


@pytest.mark.parametrize("scope", ["user", "system"])
def test_list_timers_parses_the_recording(scope: str, fixture_runner: FixtureRunner) -> None:
    result = core.list_timers(scope, runner=fixture_runner)
    rows = fixture_rows(core.list_timers_argv(scope))
    assert result.scope == scope
    assert len(result.timers) == len(rows) > 0
    for timer, row in zip(result.timers, rows):
        assert vars(timer) == {
            k: row[k] for k in ("unit", "activates", "next", "left", "last", "passed")
        }


def test_timers_keep_null_times_as_none(fixture_runner: FixtureRunner) -> None:
    rows = fixture_rows(core.list_timers_argv("system"))
    nulls = sum(1 for r in rows if r["next"] is None)
    result = core.list_timers("system", runner=fixture_runner)
    assert sum(1 for t in result.timers if t.next is None) == nulls


def test_result_objects_carry_their_list_in_a_named_field(fixture_runner: FixtureRunner) -> None:
    units = core.list_units("user", runner=fixture_runner).to_dict()
    timers = core.list_timers("user", runner=fixture_runner).to_dict()
    assert sorted(units) == ["scope", "units"]
    assert sorted(timers) == ["scope", "timers"]
