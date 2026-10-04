# journalmcp architecture

journalmcp is a read-only view of systemd and journald: eight operations that
list units and timers, show one unit's state and unit file, read the journal and
the boot list, and summarise the machine. It is one core library of functions
over typed records, with two thin fronts: a command-line tool and an MCP server
over stdio. The fronts parse arguments and render results; only the core's
real runner starts a process.

## The picture: one core, two fronts

```
 ./journalmcp <command>                 MCP client
        |                                   |
     cli.py                       tools/journalmcp-mcp   (env -i wrapper)
        |                                   |
        |                          ./journalmcp serve
        |                                   |
        |                               server.py
        +----------------+------------------+
                         |
                      core.py     validate caller values, build argv,
                         |        parse output, redact, cap
                         |  Runner.run(argv, timeout) -> RunResult
            +------------+-------------+
     SubprocessRunner            FixtureRunner
   (systemctl, journalctl)    (tests/fixtures replay)
```

| Module | Role |
|---|---|
| `src/journalmcp/core.py` | The eight operations, the validators, every argv builder, the parsers, the field allowlist and the byte caps. The one place that names a command. |
| `src/journalmcp/runner.py` | The `Runner` protocol, `SubprocessRunner` (the only code that starts a process) and `FixtureRunner`. |
| `src/journalmcp/redaction.py` | One function over text, applied to every string the fronts emit from the journal, from unit files and from unit properties. |
| `src/journalmcp/errors.py` | The typed errors. |
| `src/journalmcp/cli.py` | Argument parsing and text rendering for the CLI; calls the core and prints. |
| `src/journalmcp/server.py` | The MCP front: one tool per operation, one prompt, no logic of its own. |
| `./journalmcp` | The launcher script that runs `cli.main` from a checkout. |
| `tools/` | The fixture capture tool, the MCP wrapper, the wrapper's smoke test and the export checks. |

Both fronts return the core's result object unchanged: the CLI prints it under
`--json` and the MCP server returns it as structured content, so the two emit
the same JSON for the same call. The table in "Tool to core" below maps every
MCP tool to its core call and CLI subcommand.

### Imports

Every module under `src/journalmcp/` imports only the standard library and the
package itself, with one exception: `server.py` also imports `mcp` (the MCP
SDK) and `pydantic` (the SDK's own validation library, used for the strict
`lines` type described under "SDK and typing"). `cli.py` imports `server`
only inside `serve`, so `./journalmcp --version` and the eight query
subcommands never load either package and run under the system interpreter
with no virtual environment. Three tests hold this:
`test_hardening_every_import_is_the_standard_library_or_this_package` (whose
`ALLOWED_THIRD_PARTY` table names `mcp` and `pydantic` for `server.py` and for
no other module), `test_hardening_only_the_server_imports_the_mcp_sdk` and
`test_hardening_the_cli_imports_the_server_only_inside_serve`, all in
`tests/test_hardening.py`.

## Core, runners, fixtures and the CLI: `units`, `failed`, `timers`

Each decision below is recorded with the reason it was taken.

### Python floor: `>=3.12`

`requires-python = ">=3.12"`, with ruff's `target-version = "py312"` and mypy's
`python_version = "3.12"`. 3.12 is the Python on GitHub's `ubuntu-24.04` runner,
the oldest interpreter that ever runs this code, so the floor is a version a
test actually exercises. The mcp SDK's own floor (3.10) is lower, so nothing is
excluded that the SDK supports and CI could check. Pinning the linters to the
floor stops a newer local interpreter (3.14) from leaking newer syntax into the
tree. `uv` resolves the locked environment on 3.12 as well.

There is no `[build-system]` table: `uv sync` treats the project as virtual and
installs only the pinned dev tools (`pytest`, `ruff`, `mypy`, each `==` the
version in `uv.lock`). The package is reached through `pythonpath = ["src"]`
under pytest and through the `./journalmcp` launcher everywhere else.

### The launcher

`./journalmcp` is a `python3` script that puts its own `src/` first on
`sys.path` and calls `journalmcp.cli.main`, so it runs from any working
directory under the system interpreter with no virtual environment.
`python3 -m journalmcp` cannot work from a checkout: the package lives under
`src/`, and the system Python is externally managed (PEP 668), so there is no
supported install. The CLI and everything `--version` touches import only the
standard library.

The launcher, the capture tool and the test `conftest.py` set
`sys.dont_write_bytecode = True`. Bytecode records the absolute path of its
source file, which names the owner of the checkout, and the checks that keep
live machine values out of the tree scan `src/` as a whole, ignored caches
included.

### `Runner` protocol

```python
@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str

class Runner(Protocol):
    def run(self, argv: list[str], timeout: float) -> RunResult: ...
```

One method: an argv list and a timeout in seconds in, a typed result out. Two
implementations, both in `src/journalmcp/runner.py`:

- `SubprocessRunner` calls `subprocess.run(argv, capture_output=True, text=True,
  encoding="utf-8", errors="replace", timeout=timeout, check=False)`. `shell` is
  never passed, so it stays `False`; no command is ever a string. The explicit
  `encoding` and `errors="replace"` mean a stray byte in a unit file or a log
  line decodes to U+FFFD instead of raising `UnicodeDecodeError`, which is
  neither an `OSError` nor a typed error and would reach a front as a crash.
- `FixtureRunner(directory)` replays recordings (below). An argv with no
  recording raises `FixtureMissing`, naming the argv; it never returns empty
  output, because a test double that fails soft is a guard that cannot fail.

A runner may raise `subprocess.TimeoutExpired` or `OSError`. The conversion to
typed errors lives in the core's call helpers, not in the real runner, so a
test stub that raises `TimeoutExpired` exercises the same path the real runner
does: `core._run_raw` turns `TimeoutExpired` into `CommandTimeout` and
`OSError` (a missing binary, say) into `CommandFailed`, and `core._run` adds
that a non-zero exit becomes `CommandFailed` with the first line of stderr.
`system_summary` calls `_run_raw` directly for `systemctl is-system-running`,
because that command exits non-zero for most of its answers (see "What systemd
prints").

### Typed errors

`src/journalmcp/errors.py`: `JournalmcpError` is the base; `InvalidArgument`
(validation, raised before any argv exists), `CommandTimeout`, `CommandFailed`,
`MalformedOutput`, `FixtureMissing` and `ConfigurationError` (an environment
setting the core reads, such as `JOURNALMCP_REDACT`, holds a value it does not
accept). A front catches the base class alone.

### Timeout default: 10 seconds

`core.DEFAULT_TIMEOUT = 10.0`, passed on every call. The largest call so far,
the system-scope `list-units --all`, measured 0.01 s (three runs, `time`) on the
development machine; ten seconds is three orders of magnitude of headroom and
still short enough that a wedged systemd cannot hang a caller or an MCP client.

### Record types: frozen dataclasses

Records and results are frozen dataclasses rather than `TypedDict`: they are
constructed once by the parser and never mutated, a dataclass gives the
renderer attribute access with mypy checking, and `dataclasses.asdict` gives
the one JSON shape both fronts emit.

| Type | Fields |
|---|---|
| `Unit` | `unit`, `load`, `active`, `sub`, `description` (all `str`) |
| `Timer` | `unit`, `activates` (`str`); `next`, `left`, `last`, `passed` (`int \| None`, microseconds as systemd prints them; `None` where systemd prints `null`, a timer that never ran or is not scheduled) |
| `UnitsResult` | `scope: str`, `units: list[Unit]` |
| `TimersResult` | `scope: str`, `timers: list[Timer]` |
| `JournalResult` | `scope: str`, `unit: str \| None`, `entries: list[dict[str, str]]` (allowlisted journal fields to text, oldest first), `truncated: bool` |
| `UnitStatusResult` | `scope`, `unit`, `load_state`, `active_state`, `sub_state`, `description` (`str`); `result: str \| None`, `exec_main_pid: int \| None` (`None` when `systemctl show` prints no such property: a timer has no main process); `entries`, `truncated` as in `JournalResult` |
| `UnitFileResult` | `scope`, `unit`, `text` (`str`, redacted and byte-capped), `truncated: bool` |
| `Boot` | `index: int` (0 is the current boot, negative indexes earlier ones), `boot_id: str`, `first_entry`, `last_entry` (`int`, microseconds) |
| `BootsResult` | `boots: list[Boot]` |
| `SystemSummary` | `state: str` (one of `SYSTEM_STATES`), `failed_units_system`, `failed_units_user`, `timers_system`, `timers_user` (`int`), `boot_time: int \| None` (the current boot's first entry) |

Each operation returns one result object even though its payload is a list: the
list sits in a named field, because the MCP SDK wraps a bare list under a
`result` key and the two fronts must emit the same object. `to_dict()` is that
object. The parser checks every field's type and raises `MalformedOutput`
rather than passing an unexpected shape through.

A unit's `description` is free text its author wrote (a transient unit's is its
command line), so `list_units`, `failed_units` and `unit_status` redact it like
every other string the core emits.

The timer fields are kept raw. On systemd 259, `list-timers --output=json`
prints `left` as the same absolute timestamp as `next`, not a remaining
duration; the record does not reinterpret it.

### Scope

`scope` is exactly `"user"` or `"system"`; any other value (`"User"`, `""`,
`"system "`, `None`) raises `InvalidArgument` before an argv is built. `user`
adds `--user` after `systemctl`; `system` adds nothing.

### Read verbs and argv shapes

Every argv is `systemctl [--user] <verb> ...` or `journalctl [--user] <form>
...`, with `<verb>` or `<form>` taken from `core.READ_VERBS`, the one list in
the package that names a verb. It is `SYSTEMCTL_READ_VERBS` (`list-units`,
`list-timers`, `show`, `cat`, `is-system-running`) followed by
`JOURNALCTL_READ_FORMS` (`--output=json`, `--list-boots`): for journalctl the
first token after the optional `--user` names what the command does, a read
of the journal or the boot list. `test_every_argv_starts_with_a_read_verb`
(`tests/test_core.py`) builds the argv of all eight operations and asserts
that token is on the list for its program, and `test_argv_shapes_are_exact`
pins the exact argv of the `list-units` family.

| Operation | argv |
|---|---|
| `list_units(scope, state=None, type=None, pattern=None)` | `systemctl [--user] list-units --output=json --all --no-pager [--state=<state>] [--type=<type>] [-- <pattern>]` |
| `failed_units(scope)` | `systemctl [--user] list-units --output=json --all --no-pager --state=failed` |
| `list_timers(scope)` | `systemctl [--user] list-timers --output=json --all --no-pager` |
| `journal(scope, unit=None, since=None, until=None, priority=None, grep=None, lines=100)` | `journalctl [--user] --output=json --no-pager --lines=<lines> [--unit=<unit>] [--since=<since>] [--until=<until>] [--priority=<priority>] [--grep=<grep>]` |
| `unit_status(unit, scope, lines=20)` | `systemctl [--user] show --no-pager --property=ActiveState,SubState,LoadState,Description,ExecMainPID,Result -- <unit>`, then the `journal` argv with `--unit=<unit>` and `--lines=<lines>` |
| `unit_file(unit, scope)` | `systemctl [--user] cat --no-pager -- <unit>` |
| `boots()` | `journalctl --list-boots --output=json` |
| `system_summary()` | `systemctl is-system-running`, then the `failed_units` and `list_timers` argv for both scopes and the `boots` argv |

`boots` and `system_summary` take no scope, so their argv never carries
`--user`. `unit_status` runs two commands (`show`, then the journal query) and
`system_summary` runs six; each goes through the same runner with the same
timeout.

Flags were checked against `systemctl --help` and `man systemctl` on systemd
259: `--user`, `-t/--type=`, `--state=`, `-a/--all`, `--no-pager`, and
`list-units [PATTERN...]` / `list-timers [PATTERN...]`. `-o/--output=` is
documented for the journal output of `status`, but on systemd 259 `list-units`
and `list-timers` honour `--output=json` and print a JSON array (read back on
the same machine, and recorded in the fixtures). `--all` is passed so inactive
units loaded in memory are listed too. `--` before the pattern was checked to
work: `list-units ... -- '-x'` prints `[]` and exits 0 instead of reading `-x`
as an option.

### Caller values that reach argv

- `state` and `type` must fully match `[A-Za-z0-9_.*@:][A-Za-z0-9_.*@:-]*`, at
  most 64 characters: systemd's state and type names plus the glob and template
  characters, and never a leading `-`. Each is one token (`--state=<value>`).
- `pattern` goes after a literal `--`, so a value that begins with `-` is a
  pattern, never a flag. It must be non-empty, at most 256 characters, and hold
  no control character (newline and NUL included).

Each of the three has a test with a value that starts with `-`, a value with
shell metacharacters, a value with a newline and a value with a NUL
(`test_state_cannot_carry_an_option_or_metacharacters`,
`test_type_cannot_carry_an_option_or_metacharacters`,
`test_pattern_is_one_token_after_the_separator` and
`test_pattern_rejects_control_characters_and_empty` in `tests/test_core.py`).

### Fixtures: format and index

`tests/fixtures/index.json` maps each exact argv to a file:

```json
{"fixtures": [{"argv": ["systemctl", "--user", "list-timers", "--output=json", "--all", "--no-pager"],
               "file": "systemctl_user_list-timers_output-json_all_no-pager.json"}]}
```

Each fixture file holds `{"argv", "returncode", "stdout", "stderr"}`: the
recorded argv (checked against the request on replay), the exit code, and the
scrubbed output as exact strings. Storing the exit code and stderr lets later
recordings capture a failing command without changing the runner. A recording
whose output is an empty list (`[]`) is still a recording; "missing" is decided
by the index lookup, never by empty content. File names are a slug of the argv.

### The capture-and-scrub tool

`tools/capture-fixtures -- <argv>` (run as `uv run --frozen
tools/capture-fixtures -- ...`) runs the argv once through the real runner,
scrubs stdout and stderr, re-checks the result, and writes the fixture and its
index entry. `--dry-run` scrubs and checks and writes nothing;
`--fixtures-dir` names another output directory. Before anything runs, the
tool refuses any argv the core could not have built:

- `systemctl`: the first token that is not an option (after an optional
  `--user`) must be one of `SYSTEMCTL_READ_VERBS`.
- `journalctl`: the first token after an optional `--user` must be one of
  `JOURNALCTL_READ_FORMS` (`--output=json` or `--list-boots`). journalctl has
  no verbs, so its state-changing actions (`--vacuum-time=`, `--rotate`,
  `--flush`) are options, and the check therefore also reads the options.
- For both programs, every option must be one the core emits, and no other:
  for `systemctl` `--user`, `--output=json`, `--all`, `--no-pager`, `--state=`,
  `--type=` and `--property=`; for `journalctl` `--user`, `--output=json`,
  `--list-boots`, `--no-pager`, `--lines=`, `--unit=`, `--since=`, `--until=`,
  `--priority=` and `--grep=`. An option that reaches another host (`--host`,
  `-H`, `--machine`, `-M`) or changes state is on neither list and is refused.
  Nothing after a literal `--` is read as an option.

`test_the_tool_only_records_read_commands`,
`test_the_tool_refuses_journal_mutators_remote_hosts_and_unknown_options` and
`test_every_recorded_argv_passes_the_tool_check` (`tests/test_scrub.py`) cover
this. The scrub, in order, after a first check that refuses any text with a
secret shape (below):

1. The live hostname and username, read at run time and never stored, become
   `example-host` and `exampleuser`, including the `\x2d` form systemd uses for
   a dash inside a unit name.
2. MAC addresses become `02:00:00:00:xx:yy` (locally administered). IPv4
   addresses become addresses in `192.0.2.0/24`, `198.51.100.0/24` or
   `203.0.113.0/24`; IPv6 addresses become addresses in `2001:db8::/32`.
   Loopback, unspecified and documentation addresses are kept. A candidate is
   replaced only if `ipaddress` parses it, so PCI addresses such as
   `0000:00:1f.3` are untouched.
3. The journal fields `_HOSTNAME` and `_MACHINE_ID` become `example-host` and
   `0123456789abcdef0123456789abcdef`, and the live machine id is replaced
   wherever else it appears.
4. The publish gate's term list, read from the `PATTERN=` line of
   `tools/public-gate.sh`, is applied case-insensitively. Two shape rules run
   first: a `[systemd-run]` description (a transient unit, whose description is
   its full command line) becomes `[systemd-run] COMMAND-LINE-REMOVED`, and a
   number after the word "session" gains an `example-` prefix. Each remaining
   hit is then looked up by the SHA-256 of its lowercased text in a rule table
   and replaced by a placeholder of the same shape (a unit-name word becomes
   `example-expander` or `example-mount`, a file name becomes `EXAMPLE.md`). A
   hit with no rule refuses the fixture and prints the hit. The table is keyed
   by digest because the tool is itself scanned for those terms.
5. Disk and volume identifiers (UUIDs, the 32-digit LUKS id, LVM volume and
   PV ids, NVMe EUIs and FAT volume serials) become zero-filled placeholders of
   the same shape, ending in digits taken from a hash of the real id. One id
   maps to one placeholder in every form it appears in: dashed, undashed or
   `\x2d`-escaped.
6. If `tools/scrub-private.tsv` exists, each `literal<TAB>placeholder` row is
   applied, longest literal first, case-insensitively, including right after a
   `\x2d` escape. That file is the maintainer's list of names a recording should
   not reveal (their own user units, the disk model and serial, some installed
   services). It is not part of the public export, so the published fixtures
   carry its placeholders and a fresh clone runs without it.

`--rescrub` re-applies the whole scrub to every recorded fixture in the index
(never a hand-written `synthetic-*` one) and rewrites the files it changes; a
second run changes nothing. `test_every_disk_id_in_the_recorded_fixtures_is_a_placeholder`,
`test_no_private_literal_survives_in_the_recorded_fixtures` and
`test_rescrubbing_the_recorded_fixtures_changes_nothing` hold the recorded set
to that. The last two, and the four scrub tests that need the gate's term
list, skip in a clone that has no `tools/scrub-private.tsv` or
`tools/public-gate.sh`.

Any text with a secret shape (`bearer `, `password=`, `secret=`, `token=`, an
API-key name, a private-key header) refuses the fixture outright, before any
replacement runs; there is no placeholder for a secret
(`test_a_secret_shape_refuses_the_fixture`). A last pass re-checks the finished fixture text and
its file name for the gate's terms, the secret shapes and the live values.

Every placeholder is derived from a hash of the real value (or is a constant),
so the same value always maps to the same placeholder, across runs.

`tools/check-export-terms` greps the files that ship in the public export for
the same term list, contents and file names, and prints
`check-export-terms: clean` or every hit.

### The real-runner guard and the `live` marker

`tests/conftest.py` has an autouse fixture that replaces `subprocess.run`, as
the real runner resolves it, with a function that raises `RealRunnerReached`
unless the test carries the `live` marker. An unmarked test that reaches the
real runner therefore fails loudly
(`test_the_guard_stops_an_unmarked_test_at_the_real_runner` in
`tests/test_runner.py`). The `live` marker, declared in `pyproject.toml`, is
deselected by default (`addopts = -m "not live"`), and CI never selects it.
The three `live` tests in `tests/test_live.py` run the core against the real
user manager and journal; select them by hand with `pytest -m live`.

### CLI

```
./journalmcp --version
./journalmcp units  [--scope {user,system}] [--state STATE] [--type TYPE] [--pattern PATTERN] [--json]
./journalmcp failed [--scope {user,system}] [--json]
./journalmcp timers [--scope {user,system}] [--json]
./journalmcp status    --unit UNIT [--scope {user,system}] [--lines N] [--json]
./journalmcp unit-file --unit UNIT [--scope {user,system}] [--json]
./journalmcp journal   [--unit UNIT] [--scope {user,system}] [--since SINCE] [--until UNTIL]
                       [--priority PRIORITY] [--grep GREP] [--lines N] [--json]
./journalmcp boots     [--json]
./journalmcp summary   [--json]
./journalmcp serve
```

`--scope` defaults to `user`. `--json` prints exactly the operation's result
object (`to_dict()`, indented two spaces): `{"scope": ..., "units": [...]}` or
`{"scope": ..., "timers": [...]}`, the same object the MCP tools return as
structured content. The default is a table: `UNIT LOAD ACTIVE SUB DESCRIPTION`
for units, `TIMER ACTIVATES NEXT LAST` for timers (times as UTC, `-` for none),
then a count line such as `12 units (user)`. A core error prints one line,
`journalmcp: <message>`, on stderr and exits 1; nothing prints a traceback.
Argument errors are argparse's own (usage on stderr, exit 2). `serve` is the
ninth subcommand and starts the MCP front (see "How stdio mode starts").

### CI

`.github/workflows/tests.yml`, one workflow, `permissions: contents: read`. Job
`tests` on `ubuntu-24.04`; job `tests-arm64` on `ubuntu-24.04-arm`, guarded at
job level by `if: ${{ !github.event.repository.private }}`, so it runs only on
the public repository. GitHub's hosted-runner documentation, read 2026-10-03,
lists `ubuntu-24.04-arm` for private repositories as well, as a smaller machine
billed against the account's minutes; on a public repository it is free, and
the x64 job already gates every private push. Both run on every push and pull request. No `sudo`: uv arrives with
`pipx install uv==0.12.19` (the version the lockfile was written with), and
`uv sync --frozen` installs the pinned tools. Each job sets
`PYTHONDONTWRITEBYTECODE=1`, runs `ruff check .`, `mypy --strict src` and
`pytest -q` through `uv run --frozen`, then runs the launcher exactly as a
stranger would, under `env -i` with `PATH=/usr/bin:/bin` and a throwaway
`HOME`: `./journalmcp --version`. That last step is not redundant with the
suite: pytest reaches `src/` through `pythonpath = ["src"]`, so a green suite
says nothing about whether the shipped launcher can find the package. Nothing in
CI touches systemd; the suite replays the recorded fixtures.

## The journal, status, unit files, boots and the summary

The security side of these operations (the field allowlist, redaction, the
caps and their measurements, the unit-name rules, the injection tests) is in
`docs/security-model.md`. This section records the shapes and the systemd
behaviour they rest on.

### argv: every caller value is one `--flag=value` token

The journal argv passes each caller value as one long-form token:
`--lines=`, `--unit=`, `--since=`, `--until=`, `--priority=`, `--grep=`. The
short forms (`-n 5`, `-u x`, `-p err`) would put the value in its own argv
element, where a value beginning with `-` could be read as a new option; in
the `--flag=value` form a value that begins with `-` is that flag's value and
nothing else. `--since=--version` reaches journalctl as a time it fails to
parse (`Failed to parse timestamp: --version`, exit 1), never as `--version`.
`show` and `cat` take the unit name after `--`, with every flag before it:
the name is the one positional token. Both were checked to work on systemd
259.

Flags, as `journalctl --help` prints them on systemd 259:

```
     --user                  Show the user journal for the current user
  -S --since=DATE            Show entries not older than the specified date
  -U --until=DATE            Show entries not newer than the specified date
  -u --unit=UNIT             Show logs from the specified unit
  -p --priority=RANGE        Show entries within the specified priority range
  -g --grep=PATTERN          Show entries with MESSAGE matching PATTERN
  -n --lines[=[+]INTEGER]    Number of journal entries to show
  -o --output=STRING         Change journal output mode (short, short-precise,
     --no-pager              Do not pipe output into a pager
     --list-boots            Show terse information about recorded boots
```

`man systemctl` documents `show [PATTERN...|JOB...]`, `cat PATTERN...`,
`is-system-running`, `-p, --property=`, `--user` and `--no-pager`.

`system` scope adds no flag to journalctl: it reads the journal the invoking
user may read, which includes the system journal for a member of `adm`.

### What systemd prints, and what the core does with it

- `journalctl --grep=`: "PERL-compatible regular expressions are used"
  (`man journalctl`); `journalctl --grep=x -n 1` exits 0 on a PCRE2 build, so
  `grep` stays a journalctl flag rather than a core-side filter. A bad
  pattern (`--grep="("`) prints `Bad pattern "(": missing closing
  parenthesis` and exits 1, and a bad time (`--since=bogus`) prints
  `Failed to parse timestamp: bogus` and exits 1; both reach a front as
  `CommandFailed`. A unit with no entries prints nothing and exits 0, which
  is zero entries.
- Order: "When used with --lines= (not prefixed with "+"), --reverse is
  implied" (`man journalctl`, under `--grep=`). Read back live:
  `journalctl --grep=. -n 3 -o json` printed timestamps descending and
  `journalctl -n 3 -o json` ascending. So the core sorts entries oldest first
  by `__REALTIME_TIMESTAMP` whatever order journalctl used; an entry with no
  timestamp sorts first.
- `systemctl show --property=...` prints `key=value` lines in its own order;
  the parser accepts any order. A timer has no `ExecMainPID`, so
  `exec_main_pid` (and `result`) may be `None`. A missing unit exits 0 with
  `LoadState=not-found`, which `unit_status` reports rather than failing.
- `systemctl cat --no-pager -- <missing>` prints `No files found for <unit>.`
  and exits 1, which `unit_file` raises as `CommandFailed`.
- `is-system-running`: "This returns success (exit code 0) when the system is
  fully up and running ... Failure is returned otherwise (exit code non-zero).
  In addition, the current state is printed in a short string to standard
  output" (`man systemctl`). The state table lists `initializing`,
  `starting`, `running` (exit 0), `degraded`, `maintenance`, `stopping`,
  `offline` and `unknown` (all non-zero). So `system_summary` reads the state
  word from stdout, and a non-zero exit that printed one of those words is
  that state, not a failed call. Only output that is not a state word raises
  `CommandFailed`.
- `journalctl --list-boots --output=json` prints a JSON list of objects with
  `index`, `boot_id`, `first_entry` and `last_entry`. `boot_time` in the
  summary is the `first_entry` of the boot with index 0.

### Scope on these operations

`boots()` takes no scope: boots are a property of the machine, not of a
manager. `system_summary()` takes no scope: it covers both managers, with a
failed-unit count and a timer count for each. Their CLI subcommands have no
`--scope`.

### CLI flags

The flags are spelled like the core parameters: `--unit`, `--since`,
`--until`, `--priority`, `--grep`, `--lines`, plus `--scope` (default `user`)
and `--json`. `journal --lines` defaults to 100 and `status --lines` to 20.
`--unit` is required for `status` and `unit-file` and optional for `journal`.
A value that begins with `-` must use the `=` form (`--since=-1h`): argparse
reads a separate `-1h` as an option, and the `--since` help says so.
`boots` and `summary` take only `--json`.

Text output: `journal` and `status` print one line per entry,
`TIME  UNIT  IDENTIFIER[PID]: MESSAGE` (time as UTC), then a count line such
as `20 entries (system)`; `status` heads that with the unit's load, active and
sub states, description, result and main PID. `unit-file` prints the file
text; `boots` a table `IDX BOOT ID FIRST ENTRY LAST ENTRY`; `summary` four
lines (state, failed units per scope, timers per scope, boot time). When a
result is truncated, the text ends with a `[TRUNCATED]` line. `--json` prints
the result object, the same object the MCP tools return. A validation or
configuration error prints one line, `journalmcp: <message>`, on stderr and
exits 1.

The redaction switch is not a CLI flag: it is the environment pair
`JOURNALMCP_REDACT` and `JOURNALMCP_REDACT_PATTERNS`, read by the core, so the
CLI and the MCP front share one mechanism.

## The MCP front, `serve`, the wrapper and the example config

### SDK and typing

`mcp==2.3.0` is the one declared runtime dependency (`uv add "mcp==2.3.0"`).
The lock also pins what the SDK pulls in, among them `mcp-types`, `pydantic`,
`anyio`, `starlette` and `uvicorn`; the last two serve the SDK's HTTP
transports, which this project never uses. `server.py` imports `pydantic`
directly, for one name, `Strict`, which keeps the `lines` parameter a true
integer (next section); it is the SDK's own validation library, so it adds no
package the lock did not already hold. The SDK ships `py.typed` (as does
`mcp_types`), and `mypy --strict src` passes with `server.py` in the tree, so
there is no `[[tool.mypy.overrides]]` entry.

### The server factory

`src/journalmcp/server.py` holds `build_server(runner) -> MCPServer` and
`main()`. The factory takes the runner, so tests pass the fixture runner and
`main()` passes `None` (the core's real subprocess runner). `main()` calls
`build_server().run(transport="stdio")`, with the transport written out; no
other transport is ever passed. The server is
`MCPServer("journalmcp", version=__version__, instructions=..., log_level="WARNING")`:
at the SDK's default `INFO` level an anticipated tool error (a bad scope, say)
writes a `Tool 'list_units' failed: ...` line to stderr (read back: 107 bytes
for one bad call); at `WARNING` stderr stays empty and the client still gets
the message in the result.

Every tool is a plain `def` (the SDK runs it in a worker thread), registered
with `@mcp.tool(annotations=READ_ONLY)` where
`READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)`.
`list_tools()` returns those annotations per tool, and
`test_inmemory_every_tool_is_annotated_read_only_and_closed_world` asserts both
hints on all eight.

The `lines` parameter of `unit_status` and `journal` is typed
`Annotated[int, Strict()]`, with `Strict` from `pydantic`. The SDK validates
tool arguments with pydantic in lax mode, which would turn `true` into `1` and
`"20"` or `20.0` into `20` before the core saw them; the core and the CLI
reject those, so the MCP front must too. `Strict` keeps the input schema
`integer` and refuses the coercions
(`test_inmemory_lines_that_is_not_an_integer_is_refused_like_the_core`). The
core's own `_validate_lines` still enforces the range, 1 to 1000.

### Tool to core

| MCP tool | Parameters (defaults) | Core call | CLI subcommand |
|---|---|---|---|
| `list_units` | `scope`, `state=None`, `type=None`, `pattern=None` | `core.list_units` | `units` |
| `failed_units` | `scope` | `core.failed_units` | `failed` |
| `list_timers` | `scope` | `core.list_timers` | `timers` |
| `unit_status` | `unit`, `scope`, `lines=20` (strict integer) | `core.unit_status` | `status` |
| `journal` | `scope`, `unit=None`, `since=None`, `until=None`, `priority=None`, `grep=None`, `lines=100` (strict integer) | `core.journal` | `journal` |
| `boots` | none | `core.boots` | `boots` |
| `unit_file` | `unit`, `scope` | `core.unit_file` | `unit-file` |
| `system_summary` | none | `core.system_summary` | `summary` |

The MCP prompt `triage_failed_unit` has no CLI counterpart and is not an
operation. `scope` is a required `str` wherever it appears in a tool (the CLI
defaults it to `user`); the core validates it to `user` or `system`. The
defaults are the core's own constants
(`DEFAULT_STATUS_LINES`, `DEFAULT_JOURNAL_LINES`), the same ones the CLI's
`--lines` defaults use.

Each tool returns the core's result dataclass. The SDK builds the output
schema from the dataclass and serialises the instance as the call's
`structured_content`, which equals `to_dict()`, the object the CLI prints
under `--json`; the SDK also puts the same object, as JSON text, in
`content`. A bare list or string would be wrapped under a `result` key and
break that equality, which is why every operation returns one object with its
list in a named field.

### Errors

The SDK passes a `ToolError`'s message to the client and reduces any other
exception to `Error executing tool <name>` (`ToolError` docstring in
`mcp/server/mcpserver/exceptions.py`, and the client docs' "is_error" page).
So `_call()` re-raises every `JournalmcpError` (validation, a non-zero exit, a
timeout, malformed output, a configuration error) as `ToolError(str(exc))`.
The client gets `is_error=True`, `structured_content=None` and one text block,
`Error executing tool <name>: <the core's message>`, the same message the CLI
prints after `journalmcp: `. The server keeps serving after an error.

### The prompt

`triage_failed_unit(unit, scope)`, registered with
`@mcp.prompt(title=..., description=...)`, returns one user message telling
the model to call `unit_status`, then `journal` for the unit, then
`unit_file`, and to report the likely cause. It validates both arguments with
the core's validators first, so the message never carries text the tools
would refuse. The SDK reports any exception from a prompt as an internal
error ("Internal server error", read back), so a validation failure is raised
as `MCPError(INVALID_PARAMS, <the core's message>)`, which passes through to
the client as a JSON-RPC error with that message. There is no CLI counterpart.

### How stdio mode starts: `serve`

There were two ways to start stdio mode, a `serve` subcommand or a separate
entry point; this is a subcommand, `./journalmcp serve`. It has no
`--scope` and no `--json` (the client names the scope per call). `cli._serve`
imports `journalmcp.server` inside the function, so `--version` and the eight
query subcommands never load the SDK and still run under the system
interpreter with no `.venv`. Under the system interpreter `serve` itself
prints one line, `journalmcp: serve needs the mcp package (mcp); ...`, and
exits 1. The wrapper, the example config and the docs all start the server
through `serve`.

### The wrapper: `tools/journalmcp-mcp`

Bash, `set -euo pipefail`, `shellcheck` clean. The checkout is
`REPO="${JOURNALMCP_REPO:-$HOME/Projects/journalmcp}"`; the file holds no
literal home path or user name. It refuses with one line on stderr (exit 1)
when `$REPO/journalmcp` is missing, when `$REPO/.venv/bin/python` is missing,
or when `XDG_RUNTIME_DIR` is unset, then runs

```
exec env -i HOME=... PATH=/usr/bin:/bin XDG_RUNTIME_DIR=... [JOURNALMCP_REDACT=...] [JOURNALMCP_REDACT_PATTERNS=...] \
    "$REPO/.venv/bin/python" "$REPO/journalmcp" serve
```

Two wrapper forms were possible: this one, or
`uv run --frozen --no-sync --project`. The direct interpreter form was chosen:
it needs no `uv` binary on the restricted `PATH`, and an interpreter started
on a file cannot resolve or download anything, so the property that matters
holds by construction: nothing is resolved or downloaded at launch, which is
what launching through a package runner would do on every start.
The `.venv` is the locked environment `uv sync --frozen` builds from
`uv.lock`. The variable list and the reason for each entry are in
`docs/security-model.md`, item 10.

`JOURNALMCP_REDACT` and `JOURNALMCP_REDACT_PATTERNS` are forwarded only when
they are set in the caller's environment, because the core refuses an empty
value rather than reading it as unset.

`tools/smoke-test-mcp WRAPPER` sends one JSON-RPC `initialize` request
(protocol version `2025-11-25`, the newest in the SDK's
`HANDSHAKE_PROTOCOL_VERSIONS`; `2026-07-28` is the handshake-free
`server/discover` revision) on stdin under `timeout 30`, captures stdout and
stderr separately, and passes only when all three hold: the wrapper's exit
status is 0, stdout holds `serverInfo`, and stderr is empty. The server exits 0
when stdin closes, so a server that answers and then hangs until `timeout`
kills it (status 124) fails the check. The exit status is 0 on a pass, 1 on a
fail (both streams printed) and 2 on a usage error; the run takes under a
second.

### The example config: `docs/examples/mcp.json`

```json
{
  "mcpServers": {
    "journalmcp": {
      "command": "/home/YOUR-USER/.local/bin/journalmcp-mcp"
    }
  }
}
```

It points at the installed wrapper, never at a package runner or at the launcher;
`YOUR-USER` is a visible placeholder for the installing user's home. It lives
under `docs/examples/` rather than as a `.mcp.json` at the repo root, because
a project-scoped MCP config makes some clients ask for approval when they
start in the checkout; this file is an example to copy, not configuration the
repository applies to itself.

### Tests for this front

- `tests/test_inmemory.py` (`-k inmemory`): `async with Client(build_server(fixture_runner))`,
  marked `anyio` with the `anyio_backend` fixture in `conftest.py` returning
  `"asyncio"`. The tool set against the eight tool names, the annotations,
  the input schemas and defaults, every tool once against the fixtures, the
  prompt, redaction and both caps in this front, readable errors for a bad
  scope, a bad unit name, a failed command and a timeout, and a unit name
  beginning with `-` refused before any argv exists.
- `tests/test_parity.py` (`-k parity`): 24 CLI and MCP argument pairs, one per
  recorded fixture shape and covering all eight operations; for each, the
  CLI's `--json` (its `main` run in-process with the fixture runner) parses to
  exactly the tool's `structured_content`, and to the JSON text in `content`;
  five error pairs carry the same message in both fronts.
