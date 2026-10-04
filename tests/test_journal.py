"""The journal path: argv shape, parameter validation, the field allowlist,
the line and byte caps with their marker, redaction over every kept string,
and the timeout on every call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import StubRunner, fixture_lines, journal_line

from journalmcp import core
from journalmcp.errors import InvalidArgument, MalformedOutput
from journalmcp.redaction import PATTERNS_ENV, REDACT_ENV
from journalmcp.runner import FixtureRunner, RunResult

UNIT = "example.service"
BASE = ["journalctl", "--output=json", "--no-pager"]


def stub(*lines: str) -> StubRunner:
    return StubRunner(RunResult(0, "\n".join(lines) + ("\n" if lines else ""), ""))


def entry(i: int, message: str = "hello", **extra: object) -> str:
    fields: dict[str, object] = {
        "MESSAGE": message,
        "PRIORITY": "6",
        "SYSLOG_IDENTIFIER": "example",
        "__REALTIME_TIMESTAMP": str(1_700_000_000_000_000 + i),
        "__CURSOR": f"s=abc;i={i:x}",
        "_SYSTEMD_UNIT": UNIT,
        "_PID": str(1000 + i),
        "_COMM": "example",
        "_BOOT_ID": "0123456789abcdef0123456789abcdef",
        "_HOSTNAME": "example-host",
        "_MACHINE_ID": "0123456789abcdef0123456789abcdef",
        "_CMDLINE": "/usr/bin/example --password=hunter2",
    }
    fields.update(extra)
    return journal_line(**fields)


# --- argv shape -------------------------------------------------------------------


def test_the_default_argv_is_the_last_100_entries_as_json() -> None:
    assert core.journal_argv("system") == [*BASE, "--lines=100"]
    assert core.journal_argv("user") == ["journalctl", "--user", "--output=json", "--no-pager", "--lines=100"]
    assert core.DEFAULT_JOURNAL_LINES == 100


def test_every_caller_value_is_one_flag_equals_value_token() -> None:
    argv = core.journal_argv(
        "user", unit="cron.service", since="-1h", until="now", priority="err..crit", grep="fail", lines=5,
    )
    assert argv == [
        "journalctl", "--user", "--output=json", "--no-pager", "--lines=5",
        "--unit=cron.service", "--since=-1h", "--until=now", "--priority=err..crit", "--grep=fail",
    ]


def test_a_journal_argv_starts_with_a_read_form() -> None:
    argv = core.journal_argv("system", grep="x")
    assert argv[0] == "journalctl" and argv[1] in core.READ_VERBS


# --- parameter validation ----------------------------------------------------------


@pytest.mark.parametrize("lines", [1, 100, 1000])
def test_lines_within_the_fixed_range_pass(lines: int) -> None:
    assert f"--lines={lines}" in core.journal_argv("system", lines=lines)
    assert core.JOURNAL_LINES_MAX == 1000


@pytest.mark.parametrize("lines", [0, -1, 1001, True, "5", 2.0, None])
def test_lines_outside_the_range_or_not_an_int_are_rejected(lines: object) -> None:
    with pytest.raises(InvalidArgument, match="lines"):
        core.journal_argv("system", lines=lines)


@pytest.mark.parametrize("value", ["2026-10-03 12:00", "2026-10-03T12:00:00", "yesterday", "-1h", "+2 days", "now"])
def test_since_and_until_accept_systemd_time_words(value: str) -> None:
    argv = core.journal_argv("system", since=value, until=value)
    assert f"--since={value}" in argv and f"--until={value}" in argv


@pytest.mark.parametrize("value", ["", "a" * 65, "now; id", "a\nb", "a\x00b", "@1700000000", "1h/2", 5])
def test_since_and_until_reject_values_outside_the_charset(value: object) -> None:
    with pytest.raises(InvalidArgument, match="since"):
        core.journal_argv("system", since=value)
    with pytest.raises(InvalidArgument, match="until"):
        core.journal_argv("system", until=value)


@pytest.mark.parametrize(
    "value",
    ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug", "0", "7", "err..crit", "3..5", "warning..0"],
)
def test_priority_accepts_the_eight_names_the_digits_and_a_range(value: str) -> None:
    assert f"--priority={value}" in core.journal_argv("system", priority=value)


@pytest.mark.parametrize("value", ["8", "bogus", "err..", "..err", "err..crit..debug", "ERR", "", "3;4", 3])
def test_priority_rejects_anything_else(value: object) -> None:
    with pytest.raises(InvalidArgument, match="priority"):
        core.journal_argv("system", priority=value)


@pytest.mark.parametrize("value", ["fail", r"^Started .*\.service$", "-x", "(a|b)+", "a" * 256])
def test_grep_accepts_a_bounded_printable_pattern(value: str) -> None:
    assert f"--grep={value}" in core.journal_argv("system", grep=value)


@pytest.mark.parametrize("value", ["", "a" * 257, "a\nb", "a\x00b", "a\tb", 7])
def test_grep_rejects_empty_oversized_or_control_characters(value: object) -> None:
    with pytest.raises(InvalidArgument, match="grep"):
        core.journal_argv("system", grep=value)


def test_a_bad_scope_is_rejected_before_any_argv() -> None:
    s = stub()
    with pytest.raises(InvalidArgument):
        core.journal("User", runner=s)
    assert s.calls == []


# --- the parser and the allowlist -------------------------------------------------


def test_allowlist_is_exactly_the_recorded_keys() -> None:
    assert core.JOURNAL_FIELDS == (
        "MESSAGE", "PRIORITY", "SYSLOG_IDENTIFIER", "__REALTIME_TIMESTAMP", "__CURSOR",
        "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT", "UNIT", "USER_UNIT", "_PID", "_COMM", "_BOOT_ID",
    )
    for excluded in ("_HOSTNAME", "_MACHINE_ID", "_CMDLINE"):
        assert excluded not in core.JOURNAL_FIELDS


def test_allowlist_drops_every_key_not_on_it() -> None:
    [parsed] = core.parse_journal(entry(1) + "\n")
    assert set(parsed) <= set(core.JOURNAL_FIELDS)
    for excluded in ("_HOSTNAME", "_MACHINE_ID", "_CMDLINE"):
        assert excluded not in parsed
    assert parsed["MESSAGE"] == "hello" and parsed["_SYSTEMD_UNIT"] == UNIT
    assert "hunter2" not in json.dumps(parsed)


def test_allowlist_applies_to_the_emitted_result_too() -> None:
    result = core.journal("system", unit=UNIT, runner=stub(entry(1), entry(2)))
    assert all(set(e) <= set(core.JOURNAL_FIELDS) for e in result.entries)
    assert sorted(result.to_dict()) == ["entries", "scope", "truncated", "unit"]


def test_empty_output_is_zero_entries() -> None:
    result = core.journal("system", unit=UNIT, runner=stub())
    assert result.entries == [] and result.truncated is False


def test_a_binary_message_is_decoded_and_a_repeated_field_is_joined() -> None:
    line = journal_line(MESSAGE=[104, 105, 255], SYSLOG_IDENTIFIER=["a", "b"], PRIORITY=None, __REALTIME_TIMESTAMP="1")
    [parsed] = core.parse_journal(line)
    assert parsed["MESSAGE"] == "hi�"
    assert parsed["SYSLOG_IDENTIFIER"] == "a\nb"
    assert "PRIORITY" not in parsed


@pytest.mark.parametrize("text", ["not json\n", "[1, 2]\n", '{"MESSAGE": {"nested": 1}}\n', '{"MESSAGE": 5}\n'])
def test_malformed_journal_output_is_a_typed_error(text: str) -> None:
    with pytest.raises(MalformedOutput):
        core.parse_journal(text)


def test_entries_are_returned_oldest_first_whatever_order_journalctl_used() -> None:
    # --grep with --lines makes journalctl print newest first; the core undoes that.
    result = core.journal("system", unit=UNIT, grep="hello", runner=stub(entry(3), entry(1), entry(2)))
    assert [e["__REALTIME_TIMESTAMP"][-1] for e in result.entries] == ["1", "2", "3"]


def test_an_entry_without_a_timestamp_sorts_first_and_is_kept() -> None:
    no_stamp = journal_line(MESSAGE="early", PRIORITY="6")
    result = core.journal("system", unit=UNIT, runner=stub(entry(2), no_stamp, entry(1)))
    assert [e["MESSAGE"] for e in result.entries] == ["early", "hello", "hello"]


# --- caps and the truncation marker ----------------------------------------------


def test_truncation_marker_is_fixed() -> None:
    assert core.TRUNCATED_MARKER == "[TRUNCATED]"
    assert core.VALUE_BYTES_MAX == 4096
    assert core.RESULT_BYTES_MAX == 65536


def test_more_lines_than_the_cap_yields_exactly_the_cap_and_the_marker_truncated() -> None:
    result = core.journal("system", unit=UNIT, lines=5, runner=stub(*(entry(i) for i in range(8))))
    assert len(result.entries) == 5
    assert result.truncated is True
    # The newest five are kept, oldest first.
    assert [e["_PID"] for e in result.entries] == ["1003", "1004", "1005", "1006", "1007"]


def test_one_oversized_value_is_cut_with_the_marker_truncated() -> None:
    big = "x" * 5000
    result = core.journal("system", unit=UNIT, runner=stub(entry(1, message=big), entry(2)))
    cut = result.entries[0]["MESSAGE"]
    assert cut.endswith(core.TRUNCATED_MARKER)
    assert len(cut.encode()) <= core.VALUE_BYTES_MAX
    assert cut.startswith("x" * 100)
    assert result.truncated is True
    assert result.entries[1]["MESSAGE"] == "hello"


def test_a_cut_lands_on_a_utf8_boundary_truncated() -> None:
    result = core.journal("system", unit=UNIT, runner=stub(entry(1, message="é" * 5000)))
    cut = result.entries[0]["MESSAGE"]
    assert cut.encode("utf-8").decode("utf-8") == cut
    assert len(cut.encode()) <= core.VALUE_BYTES_MAX
    assert cut.endswith(core.TRUNCATED_MARKER)


def test_a_result_over_the_byte_cap_drops_the_oldest_entries_truncated() -> None:
    lines = [entry(i, message="y" * 3000) for i in range(30)]  # ~90 KB of messages
    result = core.journal("system", unit=UNIT, runner=stub(*lines))
    assert result.truncated is True
    assert 0 < len(result.entries) < 30
    assert len(json.dumps(result.to_dict()["entries"]).encode()) <= core.RESULT_BYTES_MAX
    assert result.entries[-1]["_PID"] == "1029"


def test_output_under_both_caps_carries_no_marker_truncated() -> None:
    result = core.journal("system", unit=UNIT, lines=10, runner=stub(*(entry(i) for i in range(10))))
    assert result.truncated is False
    assert core.TRUNCATED_MARKER not in json.dumps(result.to_dict())
    assert len(result.entries) == 10


# --- redaction over every kept string --------------------------------------------


def test_journal_messages_are_redacted_by_default() -> None:
    result = core.journal(
        "system", unit=UNIT,
        runner=stub(entry(1, message="login password=hunter2 ok"), entry(2, SYSLOG_IDENTIFIER="token=abcdefghijklmnop")),
    )
    assert result.entries[0]["MESSAGE"] == "login password=[REDACTED:password] ok"
    assert result.entries[1]["SYSLOG_IDENTIFIER"] == "token=[REDACTED:token]"


def test_redaction_runs_before_the_cut_so_no_partial_secret_survives_truncated() -> None:
    message = "x" * 4090 + " password=hunter2"
    result = core.journal("system", unit=UNIT, runner=stub(entry(1, message=message)))
    assert "hunter" not in result.entries[0]["MESSAGE"]


def test_journal_redaction_turns_off_through_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REDACT_ENV, "off")
    result = core.journal("system", unit=UNIT, runner=stub(entry(1, message="password=hunter2")))
    assert result.entries[0]["MESSAGE"] == "password=hunter2"


# --- timeouts ------------------------------------------------------------------------


def test_journal_passes_the_timeout_to_the_runner() -> None:
    s = stub()
    core.journal("system", runner=s, timeout=3.5)
    core.journal("system", runner=s)
    assert s.timeouts == [3.5, core.DEFAULT_TIMEOUT]


# --- the recordings -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "unit"), [("system", "cron.service"), ("user", "gcr-ssh-agent.service")],
)
def test_journal_parses_the_recordings(scope: str, unit: str, fixture_runner: FixtureRunner) -> None:
    result = core.journal(scope, unit=unit, lines=50, runner=fixture_runner)
    recorded = fixture_lines(core.journal_argv(scope, unit=unit, lines=50))
    assert result.scope == scope and result.unit == unit
    assert len(result.entries) == len(recorded) > 0
    assert result.truncated is False
    for parsed, raw in zip(result.entries, recorded):
        assert parsed["MESSAGE"] == raw["MESSAGE"]
        assert set(parsed) <= set(core.JOURNAL_FIELDS)
        assert "_HOSTNAME" not in parsed and "_MACHINE_ID" not in parsed


def test_the_result_cap_is_measured_in_utf8_bytes_not_ascii_escapes_truncated() -> None:
    # 20 entries of 1500 'é' each are 61440 UTF-8 bytes, under the 65536 cap;
    # escaped to \u00e9 by json.dumps they would be 180000 and be dropped.
    lines = "\n".join(
        journal_line(MESSAGE="é" * 1500, __REALTIME_TIMESTAMP=str(i)) for i in range(20)
    )
    result = core.journal("system", lines=100, runner=StubRunner(RunResult(0, lines, "")))
    assert len(result.entries) == 20
    assert result.truncated is False
    assert all(len(e["MESSAGE"]) == 1500 for e in result.entries)


def test_one_entry_at_the_value_cap_in_every_field_is_kept_not_emptied_truncated() -> None:
    # Eleven values of exactly VALUE_BYTES_MAX UTF-8 bytes each fit the result
    # cap; measured ASCII-escaped they would not, and the only entry would go.
    fields = {key: "é" * 2048 for key in core.JOURNAL_FIELDS if key != "__REALTIME_TIMESTAMP"}
    fields["__REALTIME_TIMESTAMP"] = "1"
    runner = StubRunner(RunResult(0, journal_line(**fields), ""))
    result = core.journal("system", lines=100, runner=runner)
    assert len(result.entries) == 1
    assert result.truncated is False
    assert len(result.entries[0]["MESSAGE"].encode("utf-8")) == core.VALUE_BYTES_MAX


# --- the synthetic recordings: crafted secrets, an overflow, an oversized entry --------

SECRETS_UNIT = "synthetic-secrets.service"
# Every crafted secret in the synthetic fixtures. All are invented strings.
INVENTED_SECRETS = (
    "hunter2",
    "s3cr3t-value",
    "abcdefghijklmnop1234",
    "MIIBOg",
    "abc.def-1234567890",
    "eyJhbGciOiJIUzI1NiJ9",
)


def test_redact_the_synthetic_secrets_recording(fixture_runner: FixtureRunner) -> None:
    result = core.journal("system", unit=SECRETS_UNIT, lines=20, runner=fixture_runner)
    messages = [e["MESSAGE"] for e in result.entries]
    assert len(messages) == 6
    assert "api_key=[REDACTED:token]" in messages[0]
    assert "session [REDACTED:token]" in messages[0]
    assert messages[1] == "Authorization: Bearer [REDACTED:bearer] accepted"
    assert messages[2] == "login ok password=[REDACTED:password] secret=[REDACTED:secret]"
    assert messages[3] == "loaded key\n[REDACTED:pem]\ndone"
    # IP addresses are kept: a ufw line is useless without them.
    assert "SRC=192.0.2.7 DST=198.51.100.3" in messages[4]
    assert messages[5] == "order cafe-42 shipped to 2001:db8::7"
    emitted = json.dumps(result.to_dict())
    for secret in INVENTED_SECRETS:
        assert secret not in emitted, secret
    recorded = fixture_lines(core.journal_argv("system", unit=SECRETS_UNIT, lines=20))
    for parsed, raw in zip(result.entries, recorded):
        assert parsed["_BOOT_ID"] == raw["_BOOT_ID"]
        assert parsed["__CURSOR"] == raw["__CURSOR"]
    assert result.truncated is False


def test_redact_patterns_file_adds_a_custom_class_to_the_recording(
    fixture_runner: FixtureRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patterns = tmp_path / "patterns"
    patterns.write_text("# order numbers\ncafe-\\d+\n", encoding="utf-8")
    monkeypatch.setenv(PATTERNS_ENV, str(patterns))
    result = core.journal("system", unit=SECRETS_UNIT, lines=20, runner=fixture_runner)
    assert result.entries[5]["MESSAGE"] == "order [REDACTED:custom] shipped to 2001:db8::7"
    assert result.entries[2]["MESSAGE"] == "login ok password=[REDACTED:password] secret=[REDACTED:secret]"


def test_redact_off_leaves_the_recording_verbatim(
    fixture_runner: FixtureRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REDACT_ENV, "off")
    result = core.journal("system", unit=SECRETS_UNIT, lines=20, runner=fixture_runner)
    assert result.entries[2]["MESSAGE"] == "login ok password=hunter2 secret=s3cr3t-value"


def test_truncated_overflow_recording_keeps_the_newest_five(fixture_runner: FixtureRunner) -> None:
    result = core.journal("system", unit="synthetic-overflow.service", lines=5, runner=fixture_runner)
    assert [e["MESSAGE"] for e in result.entries] == [f"entry {i}" for i in range(3, 8)]
    assert result.truncated is True


def test_truncated_oversized_recording_cuts_the_long_value(fixture_runner: FixtureRunner) -> None:
    result = core.journal("system", unit="synthetic-oversized.service", lines=10, runner=fixture_runner)
    first, second = (e["MESSAGE"] for e in result.entries)
    assert first.endswith(core.TRUNCATED_MARKER)
    assert len(first.encode("utf-8")) <= core.VALUE_BYTES_MAX
    assert second == "short"
    assert result.truncated is True
