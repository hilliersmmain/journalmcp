"""The runners: the fixture runner fails loudly, the real one never uses a shell."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from conftest import FIXTURES, RealRunnerReached, fixture_index

from journalmcp import runner as runner_module
from journalmcp.errors import FixtureMissing
from journalmcp.runner import FixtureRunner, RunResult, SubprocessRunner


def test_fixture_runner_raises_on_an_unrecorded_argv(fixture_runner: FixtureRunner) -> None:
    argv = ["systemctl", "--user", "list-units", "--output=json", "--no-such-flag"]
    with pytest.raises(FixtureMissing) as caught:
        fixture_runner.run(argv, 1.0)
    assert repr(argv) in str(caught.value)


def test_fixture_runner_returns_the_same_bytes_twice(fixture_runner: FixtureRunner) -> None:
    argv = fixture_index()[0]["argv"]
    first = fixture_runner.run(argv, 1.0)
    second = fixture_runner.run(argv, 1.0)
    assert first == second
    assert first.stdout.encode() == second.stdout.encode()


def test_there_are_at_least_six_recordings() -> None:
    assert len(fixture_index()) >= 6


@pytest.mark.parametrize("entry", fixture_index(), ids=lambda e: e["file"])
def test_every_index_entry_replays(entry: dict[str, Any], fixture_runner: FixtureRunner) -> None:
    assert (FIXTURES / entry["file"]).is_file()
    result = fixture_runner.run(entry["argv"], 1.0)
    record = json.loads((FIXTURES / entry["file"]).read_text(encoding="utf-8"))
    # The tool records a non-zero exit on purpose; a replay returns what was recorded.
    assert result.returncode == record["returncode"]
    assert result.stdout.strip(), "a recording is never empty output"


def test_real_runner_decodes_utf8_and_replaces_undecodable_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One non-UTF-8 byte in a command's output must not escape as UnicodeDecodeError,
    which is neither OSError nor a typed error; the runner asks for replacement."""
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "caf\ufffd", "")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    SubprocessRunner().run(["systemctl", "cat", "--no-pager", "--", "x.service"], 1.0)
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_real_runner_passes_an_argv_list_with_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 3, "out", "err")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    result = SubprocessRunner().run(["systemctl", "list-units"], 4.5)
    assert result == RunResult(3, "out", "err")
    assert seen["argv"] == ["systemctl", "list-units"]
    assert seen.get("shell", False) is False
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["timeout"] == 4.5


def test_the_guard_stops_an_unmarked_test_at_the_real_runner() -> None:
    with pytest.raises(RealRunnerReached, match="GUARD"):
        SubprocessRunner().run(["systemctl", "--user", "list-units"], 1.0)
