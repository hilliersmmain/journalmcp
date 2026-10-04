"""How the core runs a command: one protocol, a real runner and a fixture runner.

The real runner is the only place in the package that starts a process. It
passes an argv list straight to `subprocess.run` with no shell, so no caller
value is ever parsed by one.

The fixture runner replays recordings made by `tools/capture-fixtures`. It
looks an argv up in `index.json`; an argv with no recording raises
`FixtureMissing` and never returns empty output, because a test double that
fails soft is a guard that cannot fail.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from journalmcp.errors import FixtureMissing

INDEX_NAME = "index.json"


@dataclass(frozen=True)
class RunResult:
    """What one finished command left behind."""

    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """Runs one argv with a timeout in seconds and returns its result.

    A runner may raise `subprocess.TimeoutExpired` or `OSError`; the core turns
    both into typed errors, so a stub that raises them is tested the same way.
    """

    def run(self, argv: list[str], timeout: float) -> RunResult: ...


class SubprocessRunner:
    """The real runner: `subprocess.run` on an argv list, never a shell."""

    def run(self, argv: list[str], timeout: float) -> RunResult:
        # Decode as UTF-8 and replace what does not decode: one stray byte in
        # a unit file or a log line must not raise UnicodeDecodeError, which
        # is neither OSError nor a typed error.
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return RunResult(completed.returncode, completed.stdout, completed.stderr)


class FixtureRunner:
    """Replays recorded output for exactly the argv lists in the index."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        index = json.loads((directory / INDEX_NAME).read_text(encoding="utf-8"))
        self._files: dict[tuple[str, ...], str] = {
            tuple(entry["argv"]): entry["file"] for entry in index["fixtures"]
        }

    def run(self, argv: list[str], timeout: float) -> RunResult:
        name = self._files.get(tuple(argv))
        if name is None:
            raise FixtureMissing(f"no recorded fixture for argv {argv!r}")
        record = json.loads((self.directory / name).read_text(encoding="utf-8"))
        if record["argv"] != argv:
            raise FixtureMissing(
                f"fixture {name} records argv {record['argv']!r}, not {argv!r}"
            )
        return RunResult(record["returncode"], record["stdout"], record["stderr"])
