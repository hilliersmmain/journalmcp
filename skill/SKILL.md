---
name: journalmcp
description: Use when asked about this machine's systemd units, timers, failed services, boots or journal logs — "why did X fail", "what's in the journal for Y", "list my timers", "is the system degraded", "show me the unit file for Z". Runs the read-only journalmcp CLI, which queries systemctl and journalctl with validated arguments, redacts secrets from journal text and caps its output. It never starts, stops, restarts, enables or edits anything.
---

# journalmcp

`journalmcp` answers questions about systemd and the journal on this machine.
Every subcommand is read-only: it runs `systemctl` or `journalctl` with a
read verb (`list-units`, `list-timers`, `show`, `cat`, `is-system-running`,
or a journal read), never with a shell, and changes nothing. To change a
unit, tell the user the `systemctl` command to run themselves.

Run it from the checkout:

```
./journalmcp <subcommand> [flags]
```

## Subcommands

```
./journalmcp units     [--scope user|system] [--state STATE] [--type TYPE] [--pattern GLOB] [--json]
./journalmcp failed    [--scope user|system] [--json]
./journalmcp timers    [--scope user|system] [--json]
./journalmcp status    --unit UNIT [--scope user|system] [--lines N] [--json]
./journalmcp unit-file --unit UNIT [--scope user|system] [--json]
./journalmcp journal   [--unit UNIT] [--scope user|system] [--since WHEN] [--until WHEN]
                       [--priority LEVEL] [--grep REGEX] [--lines N] [--json]
./journalmcp boots     [--json]
./journalmcp summary   [--json]
```

- `units`: the units systemd has in memory. `--state` is a load, active or sub
  state (`failed`, `active`, `inactive`, `running`), `--type` a unit type
  (`service`, `timer`, `mount`), `--pattern` a glob over unit names.
- `failed`: the failed units.
- `timers`: the timers, with their next and last run times.
- `status`: one unit's load, active and sub state, its result and main PID,
  and its last journal entries (`--lines`, default 20).
- `unit-file`: the unit file text, as `systemctl cat` prints it.
- `journal`: journal entries, oldest first (`--lines`, default 100).
- `boots`: the recorded boots; index 0 is the current boot.
- `summary`: the system state word (`running`, `degraded`, ...), the
  failed-unit and timer counts for both managers, and the boot time.

## Scope

`--scope` is exactly `user` (the user's own systemd manager, the default) or
`system` (the system manager). Any other value is refused. Desktop and
per-user services are usually `user`; daemons such as `cron.service` are
`system`. If a unit is not found in one scope, try the other. `boots` and
`summary` take no scope.

## Arguments that are refused

A unit name must look like a systemd unit name: letters, digits, `:`, `-`,
`_`, `.`, `\`, at most one `@`, and a type suffix such as `.service` or
`.timer`; it may not begin with `-`. `--since` and `--until` take forms such
as `"2026-10-03 12:00"`, `yesterday` or `-1h`; a value that begins with `-`
needs the `=` form (`--since=-1h`). `--priority` is a syslog level (`err`,
`warning`, `info`, or `0` to `7`) or a range (`err..crit`). `--lines` is 1 to
1000. A refused argument prints one line, `journalmcp: <reason>`, and exits 1.

## Output

The default is a table or plain text for a person. `--json` prints the result
object instead: one JSON object whose list sits in a named field (`units`,
`timers`, `entries`, `boots`), plus `scope` and, where text can be cut,
`truncated`. Use `--json` when you need to read fields reliably.

Journal entries keep only an allowlist of fields: `MESSAGE`, `PRIORITY`,
`SYSLOG_IDENTIFIER`, `__REALTIME_TIMESTAMP` (microseconds), `__CURSOR`,
`_SYSTEMD_UNIT`, `_SYSTEMD_USER_UNIT`, `UNIT`, `USER_UNIT`, `_PID`, `_COMM`
and `_BOOT_ID`. Machine identifiers and command lines are dropped.

## Caps and the truncation marker

Output is capped so it stays a reasonable size:

- at most 1000 entries per call (`--lines`);
- 4096 bytes per value: a longer value is cut and ends in `[TRUNCATED]`;
- 65536 bytes per result: past that the oldest entries are dropped, and a
  unit file is cut.

Whenever anything was cut, `truncated` is `true` in the JSON and the text
output ends with a `[TRUNCATED]` line. When you see it, say the output was cut
and narrow the query (`--unit`, `--since`, `--priority`, `--grep`, fewer
`--lines`) rather than presenting it as complete.

## Redaction

Journal text, unit files and status properties are redacted before they are
printed: tokens and API keys, bearer credentials, `password=` and `secret=`
pairs, and PEM blocks become markers such as `[REDACTED:token]`. IP addresses
are kept, because firewall lines need them. A marker means a secret was
there; do not try to recover it.

The switch is the environment, not a flag:

```
JOURNALMCP_REDACT=off ./journalmcp journal --unit cron.service --scope system
JOURNALMCP_REDACT_PATTERNS=/path/to/patterns.txt ./journalmcp journal
```

`JOURNALMCP_REDACT` is `on` (the default) or `off`; any other value is an
error. `JOURNALMCP_REDACT_PATTERNS` names a file of extra regular expressions,
one per line, each replaced by `[REDACTED:custom]`. Turn redaction off only
when the user asks for it.

## Triage a failed unit

```
./journalmcp failed --scope system
./journalmcp status --unit NAME.service --scope system --lines 50
./journalmcp journal --unit NAME.service --scope system --priority warning --since=-1d
./journalmcp unit-file --unit NAME.service --scope system
```

Then report the likely cause, quoting the journal lines and unit-file
settings that support it, and suggest the fix for the user to apply.

## The MCP server

`./journalmcp serve` serves the same eight operations as MCP tools over stdio
(`list_units`, `failed_units`, `list_timers`, `unit_status`, `journal`,
`boots`, `unit_file`, `system_summary`) plus one prompt, `triage_failed_unit`.
Their results are the same objects `--json` prints. When the MCP tools are
connected, prefer them; otherwise use the CLI above.
