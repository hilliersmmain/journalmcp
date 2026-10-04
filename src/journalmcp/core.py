"""The core: validate caller values, build argv, run it, parse typed records.

Every function here is pure apart from the one call to its runner, and none
prints. Each operation returns one result dataclass even when its payload is a
list, so both fronts emit the same object: the CLI as `--json`, the MCP server
as structured content.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from journalmcp.errors import (
    CommandFailed,
    CommandTimeout,
    InvalidArgument,
    MalformedOutput,
)
from journalmcp.redaction import Redactor, redactor_from_env
from journalmcp.runner import Runner, RunResult, SubprocessRunner

Scope = Literal["user", "system"]
SCOPES: tuple[str, ...] = ("user", "system")

# Every argv the core builds is `systemctl [--user] <verb> ...` or
# `journalctl [--user] <form> ...` with <verb> or <form> on this one list: the
# first token after the optional `--user` names what the command does. For
# journalctl that token is `--output=json` (a read of the journal) or
# `--list-boots`. Nothing else in the package names a verb.
SYSTEMCTL_READ_VERBS: tuple[str, ...] = (
    "list-units",
    "list-timers",
    "show",
    "cat",
    "is-system-running",
)
JOURNALCTL_READ_FORMS: tuple[str, ...] = ("--output=json", "--list-boots")
READ_VERBS: tuple[str, ...] = SYSTEMCTL_READ_VERBS + JOURNALCTL_READ_FORMS

# The eleven unit types of `man systemd.unit`, and the name length it caps.
UNIT_SUFFIXES: tuple[str, ...] = (
    ".service",
    ".socket",
    ".device",
    ".mount",
    ".automount",
    ".swap",
    ".target",
    ".path",
    ".timer",
    ".slice",
    ".scope",
)
_UNIT_NAME_MAX = 255
# "ASCII letters, digits, ':', '-', '_', '.', and '\'" (man systemd.unit).
_UNIT_NAME_CHARS = re.compile(r"[A-Za-z0-9:_.\\-]+")

# The properties `unit_status` asks `systemctl show` for, in one fixed token.
STATUS_PROPERTIES: tuple[str, ...] = (
    "ActiveState",
    "SubState",
    "LoadState",
    "Description",
    "ExecMainPID",
    "Result",
)

# Seconds. The largest call here, the system-scope `list-units --all`, measured
# 0.01 s on the development machine; ten seconds is three orders of magnitude of
# headroom and still short enough that a wedged systemd cannot hang a caller.
DEFAULT_TIMEOUT = 10.0

# What a `--state=` or `--type=` value may hold: systemd's state and type names
# plus the glob and template characters. No value may begin with `-`.
_SELECTOR = re.compile(r"[A-Za-z0-9_.*@:][A-Za-z0-9_.*@:-]*")
_SELECTOR_MAX = 64
_PATTERN_MAX = 256
_GREP_MAX = 256

# Line caps: the `lines` parameter, bounded, with the two defaults.
DEFAULT_JOURNAL_LINES = 100
DEFAULT_STATUS_LINES = 20
JOURNAL_LINES_MAX = 1000

# Byte caps, chosen from measurements on the development machine (2026-10-03):
# `journalctl -n 100 -o json` was 107959 bytes with a longest line of 2503
# bytes; the same 100 entries reduced to the allowlisted fields were 48486
# bytes, the longest entry 1610 and the longest MESSAGE 1064. So 4096 bytes
# per value cuts no ordinary entry, and 65536 bytes per result holds the
# default 100 allowlisted entries with room to spare while staying a bounded
# share of an MCP client's context. A cut is always marked.
VALUE_BYTES_MAX = 4096
RESULT_BYTES_MAX = 65536
TRUNCATED_MARKER = "[TRUNCATED]"

# The journal field allowlist. A key is kept only for a named consumer:
#   MESSAGE               the log line itself
#   PRIORITY              severity, for triage and the --priority filter
#   SYSLOG_IDENTIFIER     which program logged (the prefix of short output)
#   __REALTIME_TIMESTAMP  when, rendered as UTC by the CLI
#   __CURSOR              a position to resume or de-duplicate a later query
#   _SYSTEMD_UNIT         which system unit the entry came from
#   _SYSTEMD_USER_UNIT    which user unit the entry came from
#   UNIT, USER_UNIT       the unit a manager message is about ("Started x")
#   _PID                  tells one process instance from the next (restarts)
#   _COMM                 the executable when SYSLOG_IDENTIFIER is absent
#   _BOOT_ID              which boot; joins with boots()
# Excluded, with the reason: _HOSTNAME and _MACHINE_ID identify the machine;
# _CMDLINE can carry secrets in arguments. Every other key has no consumer.
JOURNAL_FIELDS: tuple[str, ...] = (
    "MESSAGE",
    "PRIORITY",
    "SYSLOG_IDENTIFIER",
    "__REALTIME_TIMESTAMP",
    "__CURSOR",
    "_SYSTEMD_UNIT",
    "_SYSTEMD_USER_UNIT",
    "UNIT",
    "USER_UNIT",
    "_PID",
    "_COMM",
    "_BOOT_ID",
)

# `--since=` and `--until=` values: enough for "2026-10-03 12:00", "yesterday",
# "-1h" and "+2 days" (man journalctl); nothing a shell or an option parser
# could read differently, since the value is always one `--flag=value` token.
_TIME_SPEC = re.compile(r"[A-Za-z0-9 :+.-]{1,64}")
# `--priority=`: one of the eight syslog names or 0..7, or a FROM..TO range.
_LEVEL = r"(?:emerg|alert|crit|err|warning|notice|info|debug|[0-7])"
_PRIORITY = re.compile(_LEVEL + r"(?:\.\." + _LEVEL + r")?")


@dataclass(frozen=True)
class Unit:
    """One row of `systemctl list-units`."""

    unit: str
    load: str
    active: str
    sub: str
    description: str


@dataclass(frozen=True)
class Timer:
    """One row of `systemctl list-timers`. Times are microseconds, as systemd
    prints them; `None` where systemd prints null (never ran, never scheduled).
    """

    unit: str
    activates: str
    next: int | None
    left: int | None
    last: int | None
    passed: int | None


@dataclass(frozen=True)
class UnitsResult:
    scope: str
    units: list[Unit]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TimersResult:
    scope: str
    timers: list[Timer]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JournalResult:
    """Journal entries, oldest first, each a dict of allowlisted journal fields
    to their text. `truncated` is true when the core cut entries or text to
    stay within the line or byte caps; a cut value ends in TRUNCATED_MARKER."""

    scope: str
    unit: str | None
    entries: list[dict[str, str]]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UnitStatusResult:
    """`systemctl show` properties of one unit, plus its last journal entries.
    `result` and `exec_main_pid` are None when systemd prints no such property
    (a timer has no main process)."""

    scope: str
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    description: str
    result: str | None
    exec_main_pid: int | None
    entries: list[dict[str, str]]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UnitFileResult:
    """The text `systemctl cat` prints for one unit, redacted and byte-capped."""

    scope: str
    unit: str
    text: str
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Boot:
    """One row of `journalctl --list-boots`: index 0 is the current boot,
    negative indexes are earlier ones; the entries are microsecond stamps."""

    index: int
    boot_id: str
    first_entry: int
    last_entry: int


@dataclass(frozen=True)
class BootsResult:
    boots: list[Boot]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SystemSummary:
    """The manager state word, the failed-unit and timer counts for both
    scopes, and the first-entry time of the current boot (None if unknown)."""

    state: str
    failed_units_system: int
    failed_units_user: int
    timers_system: int
    timers_user: int
    boot_time: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The state words `systemctl is-system-running` prints (man systemctl, table
# "is-system-running output"); every one but `running` comes with a non-zero
# exit, which is a state, not a failed call.
SYSTEM_STATES: tuple[str, ...] = (
    "initializing",
    "starting",
    "running",
    "degraded",
    "maintenance",
    "stopping",
    "offline",
    "unknown",
)


def validate_scope(scope: object) -> Scope:
    if scope == "user":
        return "user"
    if scope == "system":
        return "system"
    raise InvalidArgument(f"scope must be 'user' or 'system', not {scope!r}")


def _validate_selector(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _SELECTOR_MAX
        or not _SELECTOR.fullmatch(value)
    ):
        raise InvalidArgument(
            f"{name} must be letters, digits and _ . * @ : - "
            f"(not starting with -), at most {_SELECTOR_MAX} characters; "
            f"got {value!r}"
        )
    return value


def _validate_text(name: str, value: object, max_len: int) -> str:
    """A bounded, non-empty string with no control character (newline and NUL
    included). Such a value is always one argv token in a position where a
    leading `-` cannot be read as an option."""
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise InvalidArgument(
            f"{name} must be a non-empty string of at most {max_len} characters; got {value!r}"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise InvalidArgument(f"{name} may not hold control characters: {value!r}")
    return value


def _validate_pattern(value: object) -> str:
    return _validate_text("pattern", value, _PATTERN_MAX)


def _validate_grep(value: object) -> str:
    return _validate_text("grep", value, _GREP_MAX)


def _validate_lines(value: object, name: str = "lines") -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= JOURNAL_LINES_MAX
    ):
        raise InvalidArgument(
            f"{name} must be an integer from 1 to {JOURNAL_LINES_MAX}; got {value!r}"
        )
    return value


def _validate_time(name: str, value: object) -> str:
    if not isinstance(value, str) or not _TIME_SPEC.fullmatch(value):
        raise InvalidArgument(
            f"{name} must be 1 to 64 characters of letters, digits, space, - : + and . "
            f"(such as '2026-10-03 12:00', 'yesterday' or '-1h'); got {value!r}"
        )
    return value


def _validate_priority(value: object) -> str:
    if not isinstance(value, str) or not _PRIORITY.fullmatch(value):
        raise InvalidArgument(
            "priority must be one of emerg alert crit err warning notice info debug "
            f"or 0 to 7, or a FROM..TO range of those; got {value!r}"
        )
    return value


def validate_unit_name(name: object) -> str:
    """Return name if it is a unit name by `man systemd.unit`, else raise.

    Each check names the rule it enforces. One rule is stricter than systemd's
    (which allows `-` anywhere in the prefix): a name may not begin with `-`,
    so no unit name can ever be read as an option by the command that gets it.
    """
    if not isinstance(name, str):
        raise InvalidArgument(f"unit must be a string, not {type(name).__name__}")
    if not name:
        raise InvalidArgument("unit name must not be empty")
    if len(name) > _UNIT_NAME_MAX:
        raise InvalidArgument(
            f"unit name must be at most {_UNIT_NAME_MAX} characters; got {len(name)}"
        )
    if name.startswith("-"):
        raise InvalidArgument(f"unit name may not begin with -: {name!r}")
    suffix = next((s for s in UNIT_SUFFIXES if name.endswith(s)), None)
    if suffix is None:
        raise InvalidArgument(
            f"unit name must end in a unit type suffix ({', '.join(UNIT_SUFFIXES)}); got {name!r}"
        )
    prefix = name[: -len(suffix)]
    if not prefix:
        raise InvalidArgument(f"unit name needs a prefix before the suffix: {name!r}")
    if prefix.count("@") > 1:
        raise InvalidArgument(f"unit name may hold at most one @: {name!r}")
    template, _, instance = prefix.partition("@")
    for part in (template, instance):
        if part and not _UNIT_NAME_CHARS.fullmatch(part):
            raise InvalidArgument(
                "unit name characters must be ASCII letters, digits, : - _ . and \\ "
                f"(with one optional @); got {name!r}"
            )
    if not template:
        raise InvalidArgument(f"unit name needs a prefix before the @: {name!r}")
    return name


def _command(program: str, scope: Scope | None, verb: str) -> list[str]:
    """`<program> [--user] <verb>`, with <verb> checked against the one list."""
    if verb not in READ_VERBS:
        raise InvalidArgument(f"{verb!r} is not a read verb")
    argv = [program]
    if scope == "user":
        argv.append("--user")
    argv.append(verb)
    return argv


def _systemctl(scope: Scope, verb: str) -> list[str]:
    return _command("systemctl", scope, verb) + ["--output=json", "--all", "--no-pager"]


def unit_status_argv(unit: object, scope: object) -> list[str]:
    # The unit is validated first, then goes after `--`: flags before it, the
    # name as the one positional token.
    name = validate_unit_name(unit)
    argv = _command("systemctl", validate_scope(scope), "show")
    argv += ["--no-pager", "--property=" + ",".join(STATUS_PROPERTIES), "--", name]
    return argv


def unit_file_argv(unit: object, scope: object) -> list[str]:
    name = validate_unit_name(unit)
    return _command("systemctl", validate_scope(scope), "cat") + ["--no-pager", "--", name]


def boots_argv() -> list[str]:
    # No scope: boots are a property of the machine, not of a manager.
    return _command("journalctl", None, "--list-boots") + ["--output=json"]


def system_summary_argv() -> list[str]:
    return _command("systemctl", None, "is-system-running")


def list_units_argv(
    scope: object,
    state: object = None,
    type: object = None,
    pattern: object = None,
) -> list[str]:
    argv = _systemctl(validate_scope(scope), "list-units")
    if state is not None:
        argv.append("--state=" + _validate_selector("state", state))
    if type is not None:
        argv.append("--type=" + _validate_selector("type", type))
    if pattern is not None:
        # After `--`, a value that begins with `-` is a pattern, never a flag.
        argv += ["--", _validate_pattern(pattern)]
    return argv


def failed_units_argv(scope: object) -> list[str]:
    return list_units_argv(scope, state="failed")


def list_timers_argv(scope: object) -> list[str]:
    return _systemctl(validate_scope(scope), "list-timers")


def journal_argv(
    scope: object,
    unit: object = None,
    since: object = None,
    until: object = None,
    priority: object = None,
    grep: object = None,
    lines: object = DEFAULT_JOURNAL_LINES,
) -> list[str]:
    """`journalctl [--user] --output=json --no-pager --lines=N [--unit=U]
    [--since=S] [--until=T] [--priority=P] [--grep=G]`.

    Every caller value is one `--flag=value` token, so a value that begins
    with `-` is that flag's value and never a new option. `user` scope adds
    `--user`; `system` adds nothing (the journal the invoking user may read).
    """
    argv = _command("journalctl", validate_scope(scope), "--output=json")
    argv += ["--no-pager", f"--lines={_validate_lines(lines)}"]
    if unit is not None:
        argv.append("--unit=" + validate_unit_name(unit))
    if since is not None:
        argv.append("--since=" + _validate_time("since", since))
    if until is not None:
        argv.append("--until=" + _validate_time("until", until))
    if priority is not None:
        argv.append("--priority=" + _validate_priority(priority))
    if grep is not None:
        argv.append("--grep=" + _validate_grep(grep))
    return argv


def _run_raw(runner: Runner | None, argv: list[str], timeout: float) -> RunResult:
    """Run argv; a timeout or a start failure becomes a typed error, the exit
    code is the caller's to read."""
    active = runner if runner is not None else SubprocessRunner()
    try:
        return active.run(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(f"{argv[0]} timed out after {timeout:g} s") from exc
    except OSError as exc:
        raise CommandFailed(f"{argv[0]} could not start: {exc}") from exc


def _run(runner: Runner | None, argv: list[str], timeout: float) -> str:
    """Run argv and return stdout; every failure becomes a typed error."""
    result = _run_raw(runner, argv, timeout)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[0] if detail else "no message"
        raise CommandFailed(f"{argv[0]} exited {result.returncode}: {reason}")
    return result.stdout


def _json_rows(text: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedOutput(f"systemctl printed invalid JSON: {exc}") from exc
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise MalformedOutput("systemctl JSON is not a list of objects")
    return rows


def _str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise MalformedOutput(f"field {key!r} is not a string: {value!r}")
    return value


def _usec(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise MalformedOutput(f"field {key!r} is not an integer or null: {value!r}")


def parse_units(text: str) -> list[Unit]:
    return [
        Unit(
            unit=_str(r, "unit"),
            load=_str(r, "load"),
            active=_str(r, "active"),
            sub=_str(r, "sub"),
            description=_str(r, "description"),
        )
        for r in _json_rows(text)
    ]


def parse_timers(text: str) -> list[Timer]:
    return [
        Timer(
            unit=_str(r, "unit"),
            activates=_str(r, "activates"),
            next=_usec(r, "next"),
            left=_usec(r, "left"),
            last=_usec(r, "last"),
            passed=_usec(r, "passed"),
        )
        for r in _json_rows(text)
    ]


def _redact_units(units: list[Unit], redactor: Redactor) -> list[Unit]:
    """A unit's description is free text its author wrote (a transient unit's
    is its command line), so it is redacted like every other string the core
    emits; `unit_status` redacts the same property."""
    return [replace(u, description=redactor.redact(u.description)) for u in units]


def list_units(
    scope: object,
    state: object = None,
    type: object = None,
    pattern: object = None,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> UnitsResult:
    argv = list_units_argv(scope, state, type, pattern)
    redactor = redactor_from_env()
    units = _redact_units(parse_units(_run(runner, argv, timeout)), redactor)
    return UnitsResult(scope=validate_scope(scope), units=units)


def failed_units(
    scope: object,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> UnitsResult:
    argv = failed_units_argv(scope)
    redactor = redactor_from_env()
    units = _redact_units(parse_units(_run(runner, argv, timeout)), redactor)
    return UnitsResult(scope=validate_scope(scope), units=units)


def list_timers(
    scope: object,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> TimersResult:
    argv = list_timers_argv(scope)
    return TimersResult(
        scope=validate_scope(scope), timers=parse_timers(_run(runner, argv, timeout))
    )


# --- the journal: parse, allowlist, redact, cap -----------------------------


def _journal_value(key: str, value: object) -> str | None:
    """A journal field as text. `journalctl -o json` prints a string, `null`
    for a field it left out, an array of byte values for non-UTF-8 data, or
    an array of strings for a field that appears more than once."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return bytes(v & 0xFF for v in value).decode("utf-8", errors="replace")
        if all(isinstance(v, str) for v in value):
            return "\n".join(value)
    raise MalformedOutput(f"journal field {key!r} has an unexpected shape: {value!r}")


def parse_journal(text: str) -> list[dict[str, str]]:
    """One JSON object per line in, a list of allowlisted-field dicts out.

    Only the keys in JOURNAL_FIELDS survive; a caller can never name a field.
    """
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedOutput(f"journalctl printed invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise MalformedOutput("journalctl printed a line that is not a JSON object")
        kept: dict[str, str] = {}
        for key in JOURNAL_FIELDS:
            if key in record:
                value = _journal_value(key, record[key])
                if value is not None:
                    kept[key] = value
        entries.append(kept)
    return entries


def _stamp(entry: dict[str, str]) -> int:
    """The realtime timestamp as an integer; an entry without one sorts first."""
    raw = entry.get("__REALTIME_TIMESTAMP", "")
    return int(raw) if raw.isdigit() else -1


def _cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut text to max_bytes of UTF-8, marker included, on a character
    boundary. Returns the text and whether it was cut."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    room = max_bytes - len(TRUNCATED_MARKER.encode("utf-8"))
    return data[:room].decode("utf-8", errors="ignore") + TRUNCATED_MARKER, True


def _cap_value(text: str) -> tuple[str, bool]:
    """One string value of a result, cut to VALUE_BYTES_MAX."""
    return _cap_text(text, VALUE_BYTES_MAX)


def _json_bytes(value: object) -> int:
    """The size of a value's JSON in UTF-8 bytes, the form the fronts emit.
    Escaped ASCII (`\\u00e9` for `é`) would count three times the bytes."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _finish_entries(
    entries: list[dict[str, str]], lines: int, redactor: Redactor
) -> tuple[list[dict[str, str]], bool]:
    """Sort oldest first, keep the newest `lines`, redact then cap every value,
    then drop the oldest entries until the JSON of the list fits the byte cap.

    Redaction runs before the cut, so a cut can never leave part of a secret.
    The sort undoes journalctl's own ordering, which is newest first when
    `--grep` and `--lines` are combined (man journalctl) and oldest first
    otherwise.
    """
    ordered = sorted(entries, key=_stamp)
    truncated = len(ordered) > lines
    kept = ordered[-lines:] if truncated else ordered
    out: list[dict[str, str]] = []
    for entry in kept:
        clean: dict[str, str] = {}
        for key, value in entry.items():
            text, cut = _cap_value(redactor.redact(value))
            truncated = truncated or cut
            clean[key] = text
        out.append(clean)
    # json.dumps of a list is "[" + ", ".join(items) + "]".
    sizes = [_json_bytes(e) for e in out]
    while out and 2 + sum(sizes) + 2 * (len(sizes) - 1) > RESULT_BYTES_MAX:
        out.pop(0)
        sizes.pop(0)
        truncated = True
    return out, truncated


def _journal(
    argv: list[str],
    scope: object,
    unit: object,
    lines: object,
    redactor: Redactor,
    *,
    runner: Runner | None,
    timeout: float,
) -> JournalResult:
    """Run a journal argv already built by `journal_argv` with the given redactor."""
    parsed = parse_journal(_run(runner, argv, timeout))
    entries, truncated = _finish_entries(parsed, _validate_lines(lines), redactor)
    return JournalResult(
        scope=validate_scope(scope),
        unit=None if unit is None else validate_unit_name(unit),
        entries=entries,
        truncated=truncated,
    )


def journal(
    scope: object,
    unit: object = None,
    since: object = None,
    until: object = None,
    priority: object = None,
    grep: object = None,
    lines: object = DEFAULT_JOURNAL_LINES,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> JournalResult:
    argv = journal_argv(scope, unit, since, until, priority, grep, lines)
    return _journal(argv, scope, unit, lines, redactor_from_env(), runner=runner, timeout=timeout)


# --- unit_status, unit_file, boots, system_summary ----------------------------


def parse_status_properties(text: str) -> dict[str, str]:
    """`key=value` lines, as `systemctl show --property=` prints them, in any order."""
    properties: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or not key:
            raise MalformedOutput(f"systemctl show printed a line without key=value: {line!r}")
        properties[key] = value
    return properties


def _required(properties: dict[str, str], key: str) -> str:
    try:
        return properties[key]
    except KeyError:
        raise MalformedOutput(f"systemctl show printed no {key}=") from None


def unit_status(
    unit: object,
    scope: object,
    lines: object = DEFAULT_STATUS_LINES,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> UnitStatusResult:
    argv = unit_status_argv(unit, scope)
    name = validate_unit_name(unit)
    valid_scope = validate_scope(scope)
    _validate_lines(lines)
    redactor = redactor_from_env()
    raw = parse_status_properties(_run(runner, argv, timeout))
    truncated = False

    def text(value: str) -> str:
        # Redact first, then cap, like every other string the core emits.
        nonlocal truncated
        clean, cut = _cap_value(redactor.redact(value))
        truncated = truncated or cut
        return clean

    pid_text = raw.get("ExecMainPID")
    if pid_text is not None and not pid_text.isdigit():
        raise MalformedOutput(f"ExecMainPID is not an integer: {pid_text!r}")
    # The same redactor serves the properties and the entries: built once per call.
    entries = _journal(
        journal_argv(valid_scope, name, lines=lines),
        valid_scope,
        name,
        lines,
        redactor,
        runner=runner,
        timeout=timeout,
    )
    return UnitStatusResult(
        scope=valid_scope,
        unit=name,
        load_state=text(_required(raw, "LoadState")),
        active_state=text(_required(raw, "ActiveState")),
        sub_state=text(_required(raw, "SubState")),
        description=text(raw.get("Description", "")),
        result=None if "Result" not in raw else text(raw["Result"]),
        exec_main_pid=None if pid_text is None else int(pid_text),
        entries=entries.entries,
        truncated=truncated or entries.truncated,
    )


def unit_file(
    unit: object,
    scope: object,
    *,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> UnitFileResult:
    argv = unit_file_argv(unit, scope)
    redactor = redactor_from_env()
    # Redact first, then cut: a cut can never leave part of a secret behind.
    text, truncated = _cap_text(redactor.redact(_run(runner, argv, timeout)), RESULT_BYTES_MAX)
    return UnitFileResult(
        scope=validate_scope(scope), unit=validate_unit_name(unit), text=text, truncated=truncated
    )


def _int(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise MalformedOutput(f"field {key!r} is not an integer: {value!r}")


def parse_boots(text: str) -> list[Boot]:
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedOutput(f"journalctl printed invalid JSON: {exc}") from exc
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise MalformedOutput("journalctl --list-boots JSON is not a list of objects")
    return [
        Boot(
            index=_int(r, "index"),
            boot_id=_str(r, "boot_id"),
            first_entry=_int(r, "first_entry"),
            last_entry=_int(r, "last_entry"),
        )
        for r in rows
    ]


def boots(*, runner: Runner | None = None, timeout: float = DEFAULT_TIMEOUT) -> BootsResult:
    return BootsResult(boots=parse_boots(_run(runner, boots_argv(), timeout)))


def _system_state(runner: Runner | None, timeout: float) -> str:
    """The state word of `is-system-running`. Every state but `running` exits
    non-zero (man systemctl), so the exit code is read only when stdout holds
    no state word."""
    argv = system_summary_argv()
    result = _run_raw(runner, argv, timeout)
    word = result.stdout.strip()
    if word in SYSTEM_STATES:
        return word
    detail = result.stderr.strip().splitlines()
    reason = detail[0] if detail else (f"printed {word!r}" if word else "printed nothing")
    raise CommandFailed(f"systemctl is-system-running exited {result.returncode}: {reason}")


def system_summary(
    *, runner: Runner | None = None, timeout: float = DEFAULT_TIMEOUT
) -> SystemSummary:
    state = _system_state(runner, timeout)
    failed_system = failed_units("system", runner=runner, timeout=timeout)
    failed_user = failed_units("user", runner=runner, timeout=timeout)
    timers_system = list_timers("system", runner=runner, timeout=timeout)
    timers_user = list_timers("user", runner=runner, timeout=timeout)
    current = next((b for b in boots(runner=runner, timeout=timeout).boots if b.index == 0), None)
    return SystemSummary(
        state=state,
        failed_units_system=len(failed_system.units),
        failed_units_user=len(failed_user.units),
        timers_system=len(timers_system.timers),
        timers_user=len(timers_user.timers),
        boot_time=None if current is None else current.first_entry,
    )
