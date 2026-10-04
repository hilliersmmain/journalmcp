# journalmcp

[![tests](https://github.com/hilliersmmain/journalmcp/actions/workflows/tests.yml/badge.svg)](https://github.com/hilliersmmain/journalmcp/actions/workflows/tests.yml)

A read-only view of systemd and the journal, for people and for models: eight
queries (units, failed units, timers, one unit's status, its unit file, the
journal, the boot list and a summary) served by one Python core through two
thin fronts, a command-line tool and an MCP server over stdio. It runs
`systemctl` and `journalctl` as the user who starts it, never through a shell,
caps what it returns, and redacts secrets from journal text before anything
leaves the process.

## Why this exists

"Why did that service fail?" is a question a model can help with, but only if
it can see the unit, its journal and its unit file. Handing it a shell for that
hands it `systemctl stop` too. journalmcp gives a model the reading half and
nothing else: every command it can build starts with a read verb from one fixed
list, and a test builds every one of them to check.

The same queries are a CLI, because the code a model calls should be code you
can run yourself and get the same answer. Both fronts call the same core
functions and emit the same JSON; parity tests compare them on the same
recordings.

## What it does

| CLI subcommand | MCP tool | What it reads |
|---|---|---|
| `units` | `list_units` | the units a manager has loaded, filtered by `--state`, `--type` or `--pattern` |
| `failed` | `failed_units` | the units in the failed state |
| `timers` | `list_timers` | the timers, what each activates, and when it last ran and next runs |
| `status` | `unit_status` | one unit's load, active and sub state, description, result and main PID, plus its last journal entries |
| `unit-file` | `unit_file` | the unit file as `systemctl cat` prints it, drop-ins included |
| `journal` | `journal` | journal entries, filtered by unit, time, priority and a message pattern |
| `boots` | `boots` | the recorded boots, current boot first |
| `summary` | `system_summary` | the manager state, failed-unit and timer counts for both managers, and the boot time |

The MCP front also offers one prompt, `triage_failed_unit`, which asks the
model to call `unit_status`, `journal` and `unit_file` for one unit, in that
order, and report the likely cause.

- **Scope.** `scope` is `user` (your `systemd --user` manager; the CLI's
  default) or `system`. Nothing else is accepted. `boots` and `summary` take no
  scope.
- **`--json`.** The CLI prints a table by default; `--json` prints the result
  object, which is the same object the MCP tool returns as structured content.
- **Caps.** `lines` (`--lines`) asks for 1 to 1000 entries (default 100 for
  `journal`, 20 for `status`). Every string value is capped at 4096 bytes and
  every result at 65536. A cut is never silent: the result carries
  `truncated: true`, a cut value ends in `[TRUNCATED]`, and the CLI's text ends
  with a `[TRUNCATED]` line.
- **Redaction.** On by default, over every string taken from the journal,
  unit files and unit properties: PEM blocks, bearer credentials, `password=`
  and `secret=` pairs, and tokens each become a visible `[REDACTED:<class>]`
  marker. IP addresses are kept, because a firewall log is useless without
  them. `JOURNALMCP_REDACT=off` turns it off; `JOURNALMCP_REDACT_PATTERNS=<file>`
  adds your own regular expressions, one per line. Any other value of the
  switch is an error, so a typo cannot pass as either setting. Redaction is
  pattern matching: [`docs/security-model.md`](docs/security-model.md) lists
  the credential shapes it does not catch in this version.

```
./journalmcp failed --scope system
./journalmcp status --unit cron.service --scope system --lines 5
./journalmcp journal --scope system --priority err --since=-1h --json
```

## How it differs from systemd-mcp

[openSUSE/systemd-mcp](https://github.com/openSUSE/systemd-mcp) is the closest
existing project, and it does more. Going by its README: it is built with
`go build` and "directly connects to systemd via its C API and so doesn't need
systemctl to run", where journalmcp is Python that runs `systemctl` and
`journalctl` as argv lists. It can change units: its tool list includes
`change_unit_state` ("start, stop, restart, reload, enable, disable"), and
writes "trigger a polkit request"; journalmcp has no write operation at all.
It installs with `sudo make install`, which places a `gatekeeper` in
`/usr/sbin` with its own systemd units and polkit policy so it can read the
whole journal; journalmcp runs from a checkout and reads only what the invoking
user can already read. It offers an HTTP transport with OAuth2 alongside stdio;
journalmcp speaks stdio only. Its tests include one "which tests authentication
with oauth2 using a keycloak container"; journalmcp's suite replays recorded
command output and touches neither systemd nor the network.

## Installing

Needs Linux with systemd, Python 3.12 or newer, `git` and
[uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/hilliersmmain/journalmcp.git
cd journalmcp
uv sync --frozen
./journalmcp --version
```

`uv sync --frozen` builds `.venv` from `uv.lock` exactly; nothing is resolved
afresh. The eight query subcommands and `--version` use only the standard
library and run under the system `python3` as well. `./journalmcp serve`, the
MCP server, needs the locked environment: under the system interpreter it
prints one line saying so and exits 1.

**Claude Code.** The server is started through a small wrapper,
`tools/journalmcp-mcp`, which runs it from the checkout's `.venv` under
`env -i` with only `HOME`, `PATH=/usr/bin:/bin`, `XDG_RUNTIME_DIR` and the two
redaction variables passed through. Copy it to a directory on your `PATH`,
point it at your checkout, and register it:

```
cp tools/journalmcp-mcp ~/.local/bin/
claude mcp add journalmcp -- ~/.local/bin/journalmcp-mcp
```

The wrapper looks for the checkout at `$HOME/Projects/journalmcp`; if yours is
elsewhere, edit the `REPO=` line in the copy, or set `JOURNALMCP_REPO` in the
server's environment. `bash tools/smoke-test-mcp ~/.local/bin/journalmcp-mcp`
sends one `initialize` request and prints `PASS` when the server answers
cleanly.

**Other MCP clients.** `docs/examples/mcp.json` is a server entry to copy into
your client's configuration. `YOUR-USER` in its path stands for your own home
directory (a JSON file can carry no comment to say so):

```json
{
  "mcpServers": {
    "journalmcp": {
      "command": "/home/YOUR-USER/.local/bin/journalmcp-mcp"
    }
  }
}
```

**The skill.** `skill/SKILL.md` teaches Claude Code to use the CLI directly.
To use it, link it into your own skills directory, for example
`ln -s "$PWD/skill" ~/.claude/skills/journalmcp`.

## Security model

[`docs/security-model.md`](docs/security-model.md) has one section per item,
each naming the code, the test and the threat it answers:

1. **Read-only.** Every argv starts with a read verb from one fixed list.
2. **argv only.** No shell, no `shell=True`, no command built by joining strings.
3. **Unit names are validated** against systemd's unit-name rules before use.
4. **Journal fields are allowlisted;** hostname, machine id and command line are dropped.
5. **Output is capped** by lines and bytes, with a visible truncation marker.
6. **Secrets are redacted** from message text; configurable; IP addresses kept.
7. **Every subprocess has a timeout** (10 seconds).
8. **No network calls;** only the MCP front imports anything outside the standard library.
9. **No privilege escalation:** no `sudo`, no `pkexec`, no setuid helper.
10. **The MCP wiring is a pinned `env -i` wrapper,** never a launch that resolves packages at start-up.

What journalmcp claims about itself, and the test that proves each claim:

- **Read-only by construction:** `test_every_argv_starts_with_a_read_verb`
  (`tests/test_core.py`) builds the argv of all eight operations and checks the
  verb; `pytest -k read_verb`.
- **One core serves a CLI and an MCP server:**
  `test_parity_cli_json_equals_mcp_structured_content` (`tests/test_parity.py`)
  runs both fronts on the same recordings and compares the data;
  `pytest -k parity`.
- **It redacts secrets from journal text:**
  `test_redact_the_synthetic_secrets_recording` (`tests/test_journal.py`) runs
  a recording of invented secrets through the core and finds none of them in
  the result; `pytest -k redact`.
- **It ships a documented security model:**
  [`docs/security-model.md`](docs/security-model.md), with the ten numbered
  sections above, each naming its tests.
- **Its tests run against recorded fixtures:**
  `test_the_guard_stops_an_unmarked_test_at_the_real_runner`
  (`tests/test_runner.py`) proves that a test reaching the real runner fails
  unless it is marked `live`; the recordings are in `tests/fixtures/`.

## Tests

```
uv run --frozen pytest --collect-only -q | grep -c "::"
```

prints 599 on a fresh clone. `uv run --frozen pytest -q` runs them; six skip
in a fresh clone, because they check the maintainer's private scrub inputs. The
tests replay command output recorded from a real machine and scrubbed of its
hostname, user names, addresses, machine id, disk and volume identifiers and
the maintainer's own unit names; a guard in `tests/conftest.py`
fails any test that reaches the real `subprocess` runner, so nothing in the run
touches systemd or the network. Three opt-in tests marked `live` run against
the real user manager (`uv run --frozen pytest -m live`); they are off by
default and CI never selects them. CI also runs `ruff check .`,
`mypy --strict src` and the launcher under an empty environment.

## What it does not do

- **No unit changes.** It cannot start, stop, restart, enable, disable, mask,
  edit or reload anything, in this version or through any parameter.
- **No privilege escalation.** It reads what the invoking user can read; a user
  outside `adm` or `systemd-journal` sees their own journal, not the system's.
- **No network transport.** The MCP server speaks stdio only: no HTTP, no
  socket, no listener.
- **No field selection beyond the parameters.** A caller cannot name journal
  fields or pass `journalctl` options; filtering goes through `unit`, `since`,
  `until`, `priority`, `grep` and `lines` only.
- **No sandbox.** Its guarantees are properties of the code, held by tests, not
  an operating-system boundary; the process has the invoking user's rights.
  What an MCP client then shows a model is the client's decision.
- **No escaping in the CLI's text view.** Log text is printed as written,
  control characters included; `--json` escapes them.

## Licence

MIT; see [`LICENSE`](LICENSE).
