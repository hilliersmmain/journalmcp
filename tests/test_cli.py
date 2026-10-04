"""The CLI front: --json is the core's result object, the table holds every row."""

from __future__ import annotations

import io
import json
import re

import pytest

from journalmcp import __version__, core
from journalmcp.cli import build_parser, main
from journalmcp.redaction import REDACT_ENV
from journalmcp.runner import FixtureRunner, RunResult


def run_cli(argv: list[str], runner: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, runner=runner, stdout=out, stderr=err)  # type: ignore[arg-type]
    return code, out.getvalue(), err.getvalue()


CRON, GCR = "cron.service", "gcr-ssh-agent.service"
SYNTH = "synthetic-secrets.service"

CASES = [
    (["units"], lambda r: core.list_units("user", runner=r)),
    (["units", "--scope", "system"], lambda r: core.list_units("system", runner=r)),
    (["failed"], lambda r: core.failed_units("user", runner=r)),
    (["failed", "--scope", "system"], lambda r: core.failed_units("system", runner=r)),
    (["timers"], lambda r: core.list_timers("user", runner=r)),
    (["timers", "--scope", "system"], lambda r: core.list_timers("system", runner=r)),
    (["status", "--unit", GCR], lambda r: core.unit_status(GCR, "user", runner=r)),
    (["status", "--unit", CRON, "--scope", "system", "--lines", "20"], lambda r: core.unit_status(CRON, "system", runner=r)),
    (["unit-file", "--unit", GCR], lambda r: core.unit_file(GCR, "user", runner=r)),
    (["unit-file", "--unit", CRON, "--scope", "system"], lambda r: core.unit_file(CRON, "system", runner=r)),
    (["journal", "--unit", GCR, "--lines", "50"], lambda r: core.journal("user", unit=GCR, lines=50, runner=r)),
    (["journal", "--unit", CRON, "--scope", "system", "--lines", "20"], lambda r: core.journal("system", unit=CRON, lines=20, runner=r)),
    (["boots"], lambda r: core.boots(runner=r)),
    (["summary"], lambda r: core.system_summary(runner=r)),
]


@pytest.mark.parametrize(("args", "call"), CASES, ids=[" ".join(a) for a, _ in CASES])
def test_json_is_exactly_the_result_object(args, call, fixture_runner: FixtureRunner) -> None:  # type: ignore[no-untyped-def]
    code, out, err = run_cli([*args, "--json"], fixture_runner)
    assert (code, err) == (0, "")
    assert json.loads(out) == call(fixture_runner).to_dict()


@pytest.mark.parametrize("scope", ["user", "system"])
def test_table_names_every_unit(scope: str, fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["units", "--scope", scope], fixture_runner)
    assert code == 0
    units = core.list_units(scope, runner=fixture_runner).units
    for unit in units:
        assert unit.unit in out
    assert out.rstrip().endswith(f"{len(units)} units ({scope})")


def test_table_names_every_failed_unit(fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["failed"], fixture_runner)
    assert code == 0
    for unit in core.failed_units("user", runner=fixture_runner).units:
        assert unit.unit in out


@pytest.mark.parametrize("scope", ["user", "system"])
def test_table_names_every_timer(scope: str, fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["timers", "--scope", scope], fixture_runner)
    assert code == 0
    for timer in core.list_timers(scope, runner=fixture_runner).timers:
        assert timer.unit in out


def test_a_core_error_is_one_line_on_stderr_and_exit_1() -> None:
    class Failing:
        def run(self, argv: list[str], timeout: float) -> RunResult:
            return RunResult(1, "", "Failed to connect to bus\n")

    code, out, err = run_cli(["units"], Failing())
    assert code == 1
    assert out == ""
    assert err.count("\n") == 1
    assert "Traceback" not in err


def test_a_bad_state_is_one_line_on_stderr_and_exit_1(fixture_runner: FixtureRunner) -> None:
    code, out, err = run_cli(["units", "--state=-x"], fixture_runner)
    assert (code, out) == (1, "")
    assert err.startswith("journalmcp: state must be")
    assert err.count("\n") == 1


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out == f"journalmcp {__version__}\n"
    assert __version__ == "0.1.0"


# --- the five subcommands this link adds ------------------------------------------


def test_the_cli_offers_all_eight_subcommands() -> None:
    # argparse sizes the help to the terminal: at a narrow width (a CI runner
    # reported about 36 columns) a long name such as unit-file sits alone on
    # its row and its help wraps to the next line, so match a row that starts
    # with the name rather than the name followed by a space.
    usage = build_parser().format_help()
    for name in ("units", "failed", "timers", "status", "unit-file", "journal", "boots", "summary"):
        assert re.search(rf"^ +{re.escape(name)}( |$)", usage, re.MULTILINE), name


def test_journal_lines_defaults_to_100_and_status_lines_to_20() -> None:
    parser = build_parser()
    assert parser.parse_args(["journal"]).lines == 100
    assert parser.parse_args(["status", "--unit", CRON]).lines == 20
    assert parser.parse_args(["journal", "--lines", "7"]).lines == 7


def test_journal_flags_are_spelled_like_the_core_parameters() -> None:
    # A value that begins with `-` goes in the `--flag=value` form, as argparse
    # (like every getopt-style parser) would otherwise read it as an option.
    ns = build_parser().parse_args(
        ["journal", "--scope", "system", "--unit", CRON, "--since=-1h", "--until", "now",
         "--priority", "err", "--grep", "fail", "--lines", "5", "--json"],
    )
    assert (ns.scope, ns.unit, ns.since, ns.until, ns.priority, ns.grep, ns.lines, ns.json) == (
        "system", CRON, "-1h", "now", "err", "fail", 5, True,
    )


@pytest.mark.parametrize("args", [["boots", "--scope", "user"], ["summary", "--scope", "system"], ["status"], ["unit-file"]])
def test_scope_free_subcommands_refuse_scope_and_unit_subcommands_require_unit(args: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(args)
    assert caught.value.code == 2


def test_a_bad_unit_name_is_one_line_on_stderr_and_exit_1(fixture_runner: FixtureRunner) -> None:
    code, out, err = run_cli(["status", "--unit=-x.service"], fixture_runner)
    assert (code, out) == (1, "")
    assert err.startswith("journalmcp: unit name") and err.count("\n") == 1
    code, out, err = run_cli(["journal", "--lines", "0"], fixture_runner)
    assert (code, out) == (1, "")
    assert err.startswith("journalmcp: lines must be") and err.count("\n") == 1


def test_journal_text_lists_every_message_and_counts_them(fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["journal", "--unit", CRON, "--scope", "system", "--lines", "20"], fixture_runner)
    assert code == 0
    result = core.journal("system", unit=CRON, lines=20, runner=fixture_runner)
    for entry in result.entries:
        assert entry["MESSAGE"].splitlines()[0] in out
    assert out.rstrip().endswith("20 entries (system)")
    assert core.TRUNCATED_MARKER not in out


def test_journal_text_ends_with_the_marker_when_truncated(fixture_runner: FixtureRunner) -> None:
    args = ["journal", "--unit", "synthetic-overflow.service", "--scope", "system", "--lines", "5"]
    code, out, _ = run_cli(args, fixture_runner)
    assert code == 0
    assert out.rstrip().endswith(core.TRUNCATED_MARKER)
    assert out.count("entry ") == 5
    code, out, _ = run_cli([*args, "--json"], fixture_runner)
    assert json.loads(out)["truncated"] is True


def test_status_text_names_the_unit_its_states_and_its_journal(fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["status", "--unit", CRON, "--scope", "system"], fixture_runner)
    assert code == 0
    for word in (CRON, "loaded", "active", "running", "20 entries"):
        assert word in out


def test_unit_file_text_is_the_file_itself(fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["unit-file", "--unit", CRON, "--scope", "system"], fixture_runner)
    result = core.unit_file(CRON, "system", runner=fixture_runner)
    assert code == 0 and out.rstrip("\n") == result.text.rstrip("\n")


def test_boots_text_names_every_boot(fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["boots"], fixture_runner)
    assert code == 0
    boots = core.boots(runner=fixture_runner).boots
    for boot in boots:
        assert boot.boot_id in out
    assert out.rstrip().endswith(f"{len(boots)} boots")


def test_summary_text_reports_the_state_and_counts(fixture_runner: FixtureRunner) -> None:
    code, out, _ = run_cli(["summary"], fixture_runner)
    result = core.system_summary(runner=fixture_runner)
    assert code == 0
    assert f"State: {result.state}" in out
    assert str(result.failed_units_system) in out and str(result.timers_user) in out


def test_redaction_reaches_the_cli_and_turns_off_through_the_environment_redact(
    fixture_runner: FixtureRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["unit-file", "--unit", SYNTH, "--scope", "system"]
    code, out, _ = run_cli(args, fixture_runner)
    assert code == 0 and "hunter2" not in out and "[REDACTED:password]" in out
    monkeypatch.setenv(REDACT_ENV, "off")
    code, out, _ = run_cli(args, fixture_runner)
    assert code == 0 and "hunter2" in out


def test_a_bad_redaction_setting_is_one_line_on_stderr_and_exit_1(
    fixture_runner: FixtureRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REDACT_ENV, "maybe")
    code, out, err = run_cli(["unit-file", "--unit", SYNTH, "--scope", "system"], fixture_runner)
    assert (code, out) == (1, "")
    assert err.startswith("journalmcp: JOURNALMCP_REDACT") and err.count("\n") == 1
