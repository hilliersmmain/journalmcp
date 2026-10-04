"""The four remaining operations: unit_status, unit_file, boots and
system_summary. Each is pure over the Runner, carries the timeout, and emits
one result object with any list in a named field.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    MapRunner,
    StubRunner,
    fixture_lines,
    fixture_rows,
    fixture_stdout,
    journal_line,
)

from journalmcp import core
from journalmcp.errors import CommandFailed, InvalidArgument, MalformedOutput
from journalmcp.runner import FixtureRunner, RunResult

UNIT = "example.service"
PROPS = "--property=ActiveState,SubState,LoadState,Description,ExecMainPID,Result"
SHOW = (
    "Description=Example daemon\nLoadState=loaded\nActiveState=active\n"
    "SubState=running\nResult=success\nExecMainPID=4242\n"
)
BOOTS = [
    {"index": -1, "boot_id": "b" * 32, "first_entry": 100, "last_entry": 200},
    {"index": 0, "boot_id": "c" * 32, "first_entry": 300, "last_entry": 400},
]


def lines(n: int, message: str = "hello") -> str:
    return "".join(
        journal_line(MESSAGE=message, PRIORITY="6", __REALTIME_TIMESTAMP=str(1_700_000_000_000_000 + i), _PID=str(i))
        + "\n"
        for i in range(n)
    )


def status_runner(show: str = SHOW, journal: str = lines(3), scope: str = "system", unit: str = UNIT, n: int = 20) -> MapRunner:
    return MapRunner(
        {
            tuple(core.unit_status_argv(unit, scope)): RunResult(0, show, ""),
            tuple(core.journal_argv(scope, unit=unit, lines=n)): RunResult(0, journal, ""),
        }
    )


# --- argv shapes --------------------------------------------------------------------


def test_unit_status_argv_puts_the_flags_before_the_unit_after_a_separator() -> None:
    assert core.unit_status_argv("cron.service", "system") == [
        "systemctl", "show", "--no-pager", PROPS, "--", "cron.service",
    ]
    assert core.unit_status_argv("x.timer", "user") == [
        "systemctl", "--user", "show", "--no-pager", PROPS, "--", "x.timer",
    ]


def test_unit_file_argv_is_cat_with_the_unit_after_a_separator() -> None:
    assert core.unit_file_argv("cron.service", "system") == ["systemctl", "cat", "--no-pager", "--", "cron.service"]
    assert core.unit_file_argv("x.timer", "user") == ["systemctl", "--user", "cat", "--no-pager", "--", "x.timer"]


def test_boots_argv_has_no_scope() -> None:
    assert core.boots_argv() == ["journalctl", "--list-boots", "--output=json"]


def test_system_summary_argv_is_the_state_query() -> None:
    assert core.system_summary_argv() == ["systemctl", "is-system-running"]


# --- unit_status ----------------------------------------------------------------------


def test_unit_status_parses_properties_in_any_order_and_the_journal() -> None:
    runner = status_runner()
    r = core.unit_status(UNIT, "system", runner=runner)
    assert (r.scope, r.unit) == ("system", UNIT)
    assert (r.load_state, r.active_state, r.sub_state) == ("loaded", "active", "running")
    assert (r.description, r.result, r.exec_main_pid) == ("Example daemon", "success", 4242)
    assert [e["MESSAGE"] for e in r.entries] == ["hello"] * 3
    assert r.truncated is False
    assert sorted(r.to_dict()) == [
        "active_state", "description", "entries", "exec_main_pid", "load_state",
        "result", "scope", "sub_state", "truncated", "unit",
    ]


def test_unit_status_lines_defaults_to_20_and_reaches_the_journal_argv() -> None:
    assert core.DEFAULT_STATUS_LINES == 20
    runner = status_runner()
    core.unit_status(UNIT, "system", runner=runner)
    assert runner.calls[1] == core.journal_argv("system", unit=UNIT, lines=20)
    runner = status_runner(n=5)
    core.unit_status(UNIT, "system", lines=5, runner=runner)
    assert "--lines=5" in runner.calls[1]


@pytest.mark.parametrize("value", [0, 1001, "20", True])
def test_unit_status_rejects_lines_outside_the_range(value: object) -> None:
    runner = status_runner()
    with pytest.raises(InvalidArgument, match="lines"):
        core.unit_status(UNIT, "system", lines=value, runner=runner)
    assert runner.calls == []


def test_unit_status_tolerates_a_timer_without_exec_main_pid() -> None:
    show = "Description=A timer\nLoadState=loaded\nActiveState=active\nSubState=waiting\nResult=success\n"
    r = core.unit_status("x.timer", "user", runner=status_runner(show=show, scope="user", unit="x.timer"))
    assert r.exec_main_pid is None and r.sub_state == "waiting"


def test_unit_status_of_an_unknown_unit_reports_not_found_rather_than_failing() -> None:
    show = "Description=nope.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainPID=0\n"
    r = core.unit_status("nope.service", "system", runner=status_runner(show=show, unit="nope.service", journal=""))
    assert r.load_state == "not-found" and r.exec_main_pid == 0 and r.entries == []


@pytest.mark.parametrize(
    "show",
    ["garbage without equals\n", "LoadState=loaded\nSubState=running\n", SHOW.replace("4242", "many")],
    ids=["no-equals", "missing-active-state", "pid-not-int"],
)
def test_unit_status_malformed_properties_are_a_typed_error(show: str) -> None:
    with pytest.raises(MalformedOutput):
        core.unit_status(UNIT, "system", runner=status_runner(show=show))


def test_unit_status_redacts_properties_and_entries_redact() -> None:
    show = SHOW.replace("Example daemon", "Example daemon token=abcdefghijklmnop")
    r = core.unit_status(UNIT, "system", runner=status_runner(show=show, journal=lines(1, "password=hunter2")))
    assert r.description == "Example daemon token=[REDACTED:token]"
    assert r.entries[0]["MESSAGE"] == "password=[REDACTED:password]"


def test_unit_status_caps_an_oversized_property_truncated() -> None:
    show = SHOW.replace("Example daemon", "d" * 5000)
    r = core.unit_status(UNIT, "system", runner=status_runner(show=show))
    assert r.description.endswith(core.TRUNCATED_MARKER)
    assert len(r.description.encode()) <= core.VALUE_BYTES_MAX
    assert r.truncated is True


def test_unit_status_truncated_follows_the_journal_truncated() -> None:
    r = core.unit_status(UNIT, "system", runner=status_runner(journal=lines(25)))
    assert len(r.entries) == 20 and r.truncated is True


def test_unit_status_passes_the_timeout_to_both_calls() -> None:
    runner = status_runner()
    core.unit_status(UNIT, "system", runner=runner, timeout=3.5)
    assert runner.timeouts == [3.5, 3.5]
    runner = status_runner()
    core.unit_status(UNIT, "system", runner=runner)
    assert runner.timeouts == [core.DEFAULT_TIMEOUT] * 2


def test_unit_status_validates_the_unit_before_any_call() -> None:
    runner = status_runner()
    with pytest.raises(InvalidArgument):
        core.unit_status("bad name.service", "system", runner=runner)
    assert runner.calls == []


# --- unit_file -----------------------------------------------------------------------------


def test_unit_file_returns_the_text_as_one_object() -> None:
    text = "# /etc/systemd/system/example.service\n[Service]\nExecStart=/usr/bin/example\n"
    r = core.unit_file(UNIT, "system", runner=StubRunner(RunResult(0, text, "")))
    assert (r.scope, r.unit, r.text, r.truncated) == ("system", UNIT, text, False)
    assert sorted(r.to_dict()) == ["scope", "text", "truncated", "unit"]


def test_unit_file_redacts_environment_lines_redact() -> None:
    text = "[Service]\nEnvironment=SECRET=abc123\nEnvironment=API_TOKEN=abcdefghijklmnop\nEnvironment=PATH=/usr/bin\n"
    r = core.unit_file(UNIT, "user", runner=StubRunner(RunResult(0, text, "")))
    assert "abc123" not in r.text and "abcdefghijklmnop" not in r.text
    assert "Environment=SECRET=[REDACTED:secret]" in r.text
    assert "Environment=API_TOKEN=[REDACTED:token]" in r.text
    assert "Environment=PATH=/usr/bin" in r.text


def test_unit_file_is_byte_capped_truncated() -> None:
    text = "# big\n" + "x" * 70000
    r = core.unit_file(UNIT, "system", runner=StubRunner(RunResult(0, text, "")))
    assert r.truncated is True
    assert r.text.endswith(core.TRUNCATED_MARKER)
    assert len(r.text.encode()) <= core.RESULT_BYTES_MAX


def test_unit_file_of_a_missing_unit_is_a_typed_error() -> None:
    with pytest.raises(CommandFailed, match="No files found"):
        core.unit_file("nope.service", "system", runner=StubRunner(RunResult(1, "", "No files found for nope.service.\n")))


def test_unit_file_passes_the_timeout_to_the_runner() -> None:
    runner = StubRunner(RunResult(0, "[Unit]\n", ""))
    core.unit_file(UNIT, "system", runner=runner, timeout=3.5)
    core.unit_file(UNIT, "system", runner=runner)
    assert runner.timeouts == [3.5, core.DEFAULT_TIMEOUT]


def test_unit_file_validates_the_unit_before_any_call() -> None:
    runner = StubRunner(RunResult(0, "", ""))
    with pytest.raises(InvalidArgument):
        core.unit_file("-x.service", "system", runner=runner)
    assert runner.calls == []


# --- boots -----------------------------------------------------------------------------------


def test_boots_parses_the_list_into_typed_records() -> None:
    r = core.boots(runner=StubRunner(RunResult(0, json.dumps(BOOTS), "")))
    assert [b.index for b in r.boots] == [-1, 0]
    assert r.boots[1] == core.Boot(index=0, boot_id="c" * 32, first_entry=300, last_entry=400)
    assert sorted(r.to_dict()) == ["boots"]
    assert r.to_dict()["boots"][0] == BOOTS[0]


@pytest.mark.parametrize(
    "stdout",
    ["{}", "[1]", '[{"index": 0}]', '[{"index": "0", "boot_id": "x", "first_entry": 1, "last_entry": 2}]', "nope"],
)
def test_boots_malformed_output_is_a_typed_error(stdout: str) -> None:
    with pytest.raises(MalformedOutput):
        core.boots(runner=StubRunner(RunResult(0, stdout, "")))


def test_boots_passes_the_timeout_to_the_runner() -> None:
    runner = StubRunner(RunResult(0, "[]", ""))
    core.boots(runner=runner, timeout=3.5)
    core.boots(runner=runner)
    assert runner.timeouts == [3.5, core.DEFAULT_TIMEOUT]


# --- system_summary --------------------------------------------------------------------------


def failed_rows(n: int) -> str:
    return json.dumps(
        [{"unit": f"f{i}.service", "load": "loaded", "active": "failed", "sub": "failed", "description": "x"} for i in range(n)]
    )


def timer_rows(n: int) -> str:
    return json.dumps(
        [{"unit": f"t{i}.timer", "activates": f"t{i}.service", "next": None, "left": None, "last": None, "passed": None} for i in range(n)]
    )


def summary_runner(state: RunResult | None = None, boots: list[dict[str, object]] = BOOTS) -> MapRunner:
    if state is None:
        state = RunResult(0, "running\n", "")
    failed, timers = failed_rows, timer_rows
    return MapRunner(
        {
            tuple(core.system_summary_argv()): state,
            tuple(core.failed_units_argv("system")): RunResult(0, failed(2), ""),
            tuple(core.failed_units_argv("user")): RunResult(0, failed(1), ""),
            tuple(core.list_timers_argv("system")): RunResult(0, timers(5), ""),
            tuple(core.list_timers_argv("user")): RunResult(0, timers(3), ""),
            tuple(core.boots_argv()): RunResult(0, json.dumps(boots), ""),
        }
    )


def test_system_summary_collects_the_state_the_counts_and_the_boot_time() -> None:
    r = core.system_summary(runner=summary_runner())
    assert r.state == "running"
    assert (r.failed_units_system, r.failed_units_user) == (2, 1)
    assert (r.timers_system, r.timers_user) == (5, 3)
    assert r.boot_time == 300  # first_entry of the boot with index 0, not of the last row
    assert sorted(r.to_dict()) == [
        "boot_time", "failed_units_system", "failed_units_user", "state", "timers_system", "timers_user",
    ]


@pytest.mark.parametrize("state", ["initializing", "starting", "degraded", "maintenance", "stopping", "offline", "unknown"])
def test_system_summary_reads_a_state_word_from_a_non_zero_exit(state: str) -> None:
    r = core.system_summary(runner=summary_runner(state=RunResult(1, state + "\n", "")))
    assert r.state == state


@pytest.mark.parametrize("stdout", ["banana\n", "", "running extra\n"])
def test_system_summary_rejects_anything_but_a_state_word(stdout: str) -> None:
    with pytest.raises(CommandFailed, match="is-system-running"):
        core.system_summary(runner=summary_runner(state=RunResult(1, stdout, "")))


def test_system_summary_boot_time_is_none_without_a_current_boot() -> None:
    assert core.system_summary(runner=summary_runner(boots=[])).boot_time is None


def test_system_summary_passes_the_timeout_to_every_call() -> None:
    runner = summary_runner()
    core.system_summary(runner=runner, timeout=3.5)
    assert len(runner.calls) == 6 and runner.timeouts == [3.5] * 6


# --- the recordings ------------------------------------------------------------------------------


@pytest.mark.parametrize(("scope", "unit"), [("system", "cron.service"), ("user", "gcr-ssh-agent.service")])
def test_unit_status_parses_the_recordings(scope: str, unit: str, fixture_runner: FixtureRunner) -> None:
    r = core.unit_status(unit, scope, runner=fixture_runner)
    assert (r.load_state, r.active_state, r.sub_state) == ("loaded", "active", "running")
    assert r.description and r.result == "success" and r.exec_main_pid
    assert len(r.entries) == len(fixture_lines(core.journal_argv(scope, unit=unit, lines=20))) == 20
    assert r.truncated is False


@pytest.mark.parametrize(("scope", "unit"), [("system", "cron.service"), ("user", "gcr-ssh-agent.service")])
def test_unit_file_parses_the_recordings(scope: str, unit: str, fixture_runner: FixtureRunner) -> None:
    r = core.unit_file(unit, scope, runner=fixture_runner)
    assert r.text == fixture_stdout(core.unit_file_argv(unit, scope))
    assert "[Service]" in r.text and r.truncated is False


def test_boots_parses_the_recording(fixture_runner: FixtureRunner) -> None:
    r = core.boots(runner=fixture_runner)
    recorded = json.loads(fixture_stdout(core.boots_argv()))
    assert len(r.boots) == len(recorded) > 0
    assert any(b.index == 0 for b in r.boots)
    assert all(b.first_entry <= b.last_entry for b in r.boots)


def test_system_summary_over_the_recordings(fixture_runner: FixtureRunner) -> None:
    r = core.system_summary(runner=fixture_runner)
    assert r.state == fixture_stdout(core.system_summary_argv()).strip()
    assert r.failed_units_system == len(fixture_rows(core.failed_units_argv("system")))
    assert r.failed_units_user == len(fixture_rows(core.failed_units_argv("user")))
    assert r.timers_system == len(fixture_rows(core.list_timers_argv("system")))
    assert r.timers_user == len(fixture_rows(core.list_timers_argv("user")))
    current = next(b for b in json.loads(fixture_stdout(core.boots_argv())) if b["index"] == 0)
    assert r.boot_time == current["first_entry"]


# --- the synthetic recordings with crafted secrets --------------------------------------

from test_journal import INVENTED_SECRETS, SECRETS_UNIT


def test_unit_status_redacts_the_synthetic_recording_redact(fixture_runner: FixtureRunner) -> None:
    r = core.unit_status(SECRETS_UNIT, "system", runner=fixture_runner)
    assert r.description == "Synthetic daemon token=[REDACTED:token]"
    assert len(r.entries) == 6
    emitted = json.dumps(r.to_dict())
    for secret in INVENTED_SECRETS:
        assert secret not in emitted, secret


def test_unit_file_redacts_the_synthetic_recording_redact(fixture_runner: FixtureRunner) -> None:
    r = core.unit_file(SECRETS_UNIT, "system", runner=fixture_runner)
    assert "Environment=SECRET=[REDACTED:secret]" in r.text
    assert "Environment=API_TOKEN=[REDACTED:token]" in r.text
    assert "--password=[REDACTED:password]" in r.text
    assert "Environment=PATH=/usr/bin" in r.text
    for secret in INVENTED_SECRETS:
        assert secret not in r.text, secret
