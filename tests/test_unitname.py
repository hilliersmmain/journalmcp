"""Unit names are validated against systemd's own rules before any argv exists.

The rules were read from `man systemd.unit` (systemd 259): the prefix is one or
more of ASCII letters, digits, `:`, `-`, `_`, `.` and `\\`; a template unit has
a single `@` at the end of the prefix; the whole name is at most 255 characters;
the suffix is one of eleven unit types. One rule here is stricter than systemd:
a name may not begin with `-`, so no unit name can ever read as an option.
"""

from __future__ import annotations

import pytest

from journalmcp import core
from journalmcp.errors import InvalidArgument

SUFFIXES = [
    ".service", ".socket", ".device", ".mount", ".automount", ".swap",
    ".target", ".path", ".timer", ".slice", ".scope",
]


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_each_of_the_eleven_suffixes_is_accepted(suffix: str) -> None:
    assert core.validate_unit_name("a" + suffix) == "a" + suffix


def test_the_suffix_list_is_exactly_the_eleven_types() -> None:
    assert sorted(core.UNIT_SUFFIXES) == sorted(SUFFIXES)


def test_an_instance_unit_with_one_at_sign_is_accepted() -> None:
    assert core.validate_unit_name("gnome-session@ubuntu.target") == "gnome-session@ubuntu.target"


def test_a_template_name_with_an_empty_instance_is_accepted() -> None:
    assert core.validate_unit_name("getty@.service") == "getty@.service"


def test_a_second_at_sign_is_rejected() -> None:
    with pytest.raises(InvalidArgument, match="@"):
        core.validate_unit_name("a@b@c.service")


def test_255_characters_are_accepted_and_256_are_rejected() -> None:
    suffix = ".service"
    ok = "a" * (255 - len(suffix)) + suffix
    assert len(ok) == 255
    assert core.validate_unit_name(ok) == ok
    with pytest.raises(InvalidArgument, match="255"):
        core.validate_unit_name("a" + ok)


@pytest.mark.parametrize(
    "name",
    ["", "my unit.service", "a/b.service", "-x.service", "a\nb.service", "a\x00b.service"],
    ids=["empty", "space", "slash", "leading-dash", "newline", "nul"],
)
def test_names_outside_the_charset_are_rejected(name: str) -> None:
    with pytest.raises(InvalidArgument):
        core.validate_unit_name(name)


@pytest.mark.parametrize("name", ["cron", "cron.exe", ".service", "a.Service"])
def test_a_name_without_one_of_the_eleven_suffixes_is_rejected(name: str) -> None:
    with pytest.raises(InvalidArgument, match="suffix"):
        core.validate_unit_name(name)


@pytest.mark.parametrize("name", [None, 3, b"cron.service", ["cron.service"]])
def test_a_non_string_is_rejected(name: object) -> None:
    with pytest.raises(InvalidArgument):
        core.validate_unit_name(name)


def test_the_error_names_the_rule_that_failed() -> None:
    with pytest.raises(InvalidArgument, match="begin with -"):
        core.validate_unit_name("-x.service")
    with pytest.raises(InvalidArgument, match="characters"):
        core.validate_unit_name("a b.service")
    with pytest.raises(InvalidArgument, match="suffix"):
        core.validate_unit_name("nosuffix")


def test_every_unit_taking_operation_validates_before_building_argv() -> None:
    bad = "a b.service"
    with pytest.raises(InvalidArgument):
        core.unit_status_argv(bad, "user")
    with pytest.raises(InvalidArgument):
        core.unit_file_argv(bad, "user")
    with pytest.raises(InvalidArgument):
        core.journal_argv("user", unit=bad)
