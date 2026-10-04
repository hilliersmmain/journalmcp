"""Shared fixtures, and the guard that keeps every unmarked test off systemd."""

from __future__ import annotations

import json
import sys

# No __pycache__ under src/: bytecode records the absolute path of its source,
# which names the checkout's owner, and src/ is scanned for that name.
sys.dont_write_bytecode = True

from pathlib import Path
from typing import Any

import pytest

from journalmcp import runner as runner_module
from journalmcp.errors import FixtureMissing
from journalmcp.runner import INDEX_NAME, FixtureRunner, RunResult

FIXTURES = Path(__file__).parent / "fixtures"


class RealRunnerReached(AssertionError):
    """An unmarked test reached the real subprocess runner."""


@pytest.fixture(autouse=True)
def _guard_real_runner(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real runner's subprocess call raise unless the test is `live`."""
    if request.node.get_closest_marker("live") is not None:
        return

    def refuse(argv: object, *args: object, **kwargs: object) -> None:
        raise RealRunnerReached(
            f"GUARD: unmarked test {request.node.nodeid} reached the real runner with argv {argv!r}"
        )

    monkeypatch.setattr(runner_module.subprocess, "run", refuse)


@pytest.fixture
def fixture_runner() -> FixtureRunner:
    return FixtureRunner(FIXTURES)


def fixture_index() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads((FIXTURES / INDEX_NAME).read_text())["fixtures"]
    return entries


def fixture_stdout(argv: list[str]) -> str:
    """The recorded stdout for argv, read straight from the file."""
    for entry in fixture_index():
        if entry["argv"] == argv:
            record = json.loads((FIXTURES / entry["file"]).read_text())
            stdout: str = record["stdout"]
            return stdout
    raise LookupError(f"no fixture for {argv!r}")


def fixture_rows(argv: list[str]) -> list[dict[str, Any]]:
    """The parsed JSON array recorded for argv (systemctl's list output)."""
    rows: list[dict[str, Any]] = json.loads(fixture_stdout(argv))
    return rows


def fixture_lines(argv: list[str]) -> list[dict[str, Any]]:
    """The parsed JSON objects recorded for argv, one per line (journalctl)."""
    return [json.loads(line) for line in fixture_stdout(argv).splitlines() if line.strip()]


class StubRunner:
    """Records each call (argv and timeout) and returns or raises what it was built with."""

    def __init__(self, result: RunResult | None = None, raises: BaseException | None = None):
        self.result = result
        self.raises = raises
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def run(self, argv: list[str], timeout: float) -> RunResult:
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        assert self.result is not None
        return self.result


class MapRunner:
    """Answers each exact argv with its own result; an unknown argv fails loudly."""

    def __init__(self, results: dict[tuple[str, ...], RunResult]):
        self.results = results
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def run(self, argv: list[str], timeout: float) -> RunResult:
        self.calls.append(argv)
        self.timeouts.append(timeout)
        try:
            return self.results[tuple(argv)]
        except KeyError:
            raise FixtureMissing(f"MapRunner has no result for {argv!r}") from None


def journal_line(**fields: object) -> str:
    """One journal record as `journalctl -o json` prints it: one JSON object per line."""
    return json.dumps(fields)


@pytest.fixture
def anyio_backend() -> str:
    """The MCP in-memory client runs on anyio; these tests use its asyncio backend."""
    return "asyncio"
