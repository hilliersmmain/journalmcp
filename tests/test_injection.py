"""Argument injection: one test per caller-supplied parameter that reaches
argv, across all eight operations, with a value that starts with `-`, a value
with shell metacharacters, a command substitution, a newline and a NUL.

Each asserts one of two outcomes: the value is rejected before any argv
exists, or it lands as exactly one argv element in the `--flag=value` form or
after the `--` separator, with the command in front of it unchanged. Nothing
here is ever run; the metacharacters are inert text in a list.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from journalmcp import core
from journalmcp.errors import InvalidArgument

HOSTILE = {
    "leading-dash": "--version",
    "shell-metacharacters": "; echo INJECTED; #",
    "command-substitution": "$(echo INJECTED)",
    "newline": "x\nINJECTED",
    "nul": "x\x00INJECTED",
}
VALUES = list(HOSTILE.values())
IDS = list(HOSTILE.keys())
UNIT = "example.service"


def rejected(build: Callable[[], list[str]]) -> None:
    with pytest.raises(InvalidArgument):
        build()


def one_token(argv: list[str], base: list[str], token: str) -> None:
    """The value is one element, appended to an otherwise unchanged base argv."""
    assert argv == [*base, token]
    assert argv[0] == base[0]


# --- scope, for every operation that takes one ----------------------------------------

SCOPED: list[tuple[str, Callable[[object], list[str]]]] = [
    ("list_units", lambda s: core.list_units_argv(s)),
    ("failed_units", lambda s: core.failed_units_argv(s)),
    ("list_timers", lambda s: core.list_timers_argv(s)),
    ("journal", lambda s: core.journal_argv(s)),
    ("unit_status", lambda s: core.unit_status_argv(UNIT, s)),
    ("unit_file", lambda s: core.unit_file_argv(UNIT, s)),
]


@pytest.mark.parametrize("value", VALUES, ids=IDS)
@pytest.mark.parametrize("build", [b for _, b in SCOPED], ids=[n for n, _ in SCOPED])
def test_scope_injection_is_rejected(build: Callable[[object], list[str]], value: str) -> None:
    rejected(lambda: build(value))


# --- list_units: state, type, pattern -------------------------------------------------


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_state_injection_is_rejected(value: str) -> None:
    rejected(lambda: core.list_units_argv("user", state=value))


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_type_injection_is_rejected(value: str) -> None:
    rejected(lambda: core.list_units_argv("user", type=value))


@pytest.mark.parametrize("value", VALUES[:3], ids=IDS[:3])
def test_pattern_injection_lands_after_the_separator(value: str) -> None:
    argv = core.list_units_argv("user", pattern=value)
    assert argv == [*core.list_units_argv("user"), "--", value]
    assert argv.index("--") == len(argv) - 2


@pytest.mark.parametrize("value", VALUES[3:], ids=IDS[3:])
def test_pattern_injection_with_control_characters_is_rejected(value: str) -> None:
    rejected(lambda: core.list_units_argv("user", pattern=value))


# --- unit, for each operation that takes one --------------------------------------------


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_unit_injection_in_unit_status_is_rejected(value: str) -> None:
    rejected(lambda: core.unit_status_argv(value, "user"))


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_unit_injection_in_unit_file_is_rejected(value: str) -> None:
    rejected(lambda: core.unit_file_argv(value, "user"))


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_unit_injection_in_journal_is_rejected(value: str) -> None:
    rejected(lambda: core.journal_argv("user", unit=value))


# --- journal: since, until, priority, grep, lines ------------------------------------------


def test_since_injection_leading_dash_is_one_flag_token() -> None:
    one_token(core.journal_argv("system", since="--version"), core.journal_argv("system"), "--since=--version")


@pytest.mark.parametrize("value", VALUES[1:], ids=IDS[1:])
def test_since_injection_is_rejected(value: str) -> None:
    rejected(lambda: core.journal_argv("system", since=value))


def test_until_injection_leading_dash_is_one_flag_token() -> None:
    one_token(core.journal_argv("system", until="--version"), core.journal_argv("system"), "--until=--version")


@pytest.mark.parametrize("value", VALUES[1:], ids=IDS[1:])
def test_until_injection_is_rejected(value: str) -> None:
    rejected(lambda: core.journal_argv("system", until=value))


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_priority_injection_is_rejected(value: str) -> None:
    rejected(lambda: core.journal_argv("system", priority=value))


@pytest.mark.parametrize("value", VALUES[:3], ids=IDS[:3])
def test_grep_injection_is_one_flag_token(value: str) -> None:
    one_token(core.journal_argv("system", grep=value), core.journal_argv("system"), "--grep=" + value)


@pytest.mark.parametrize("value", VALUES[3:], ids=IDS[3:])
def test_grep_injection_with_control_characters_is_rejected(value: str) -> None:
    rejected(lambda: core.journal_argv("system", grep=value))


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_lines_injection_is_rejected(value: str) -> None:
    rejected(lambda: core.journal_argv("system", lines=value))
    rejected(lambda: core.unit_status(UNIT, "system", lines=value, runner=None))


def test_every_operation_with_a_parameter_has_an_injection_test_here() -> None:
    names = {n for n in globals() if n.startswith("test_") and "injection" in n}
    for parameter in ("scope", "state", "type", "pattern", "unit", "since", "until", "priority", "grep", "lines"):
        assert any(f"test_{parameter}_injection" in n for n in names), parameter
