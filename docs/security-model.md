# journalmcp security model

journalmcp reads systemd state and the journal and hands the result to a human
at a terminal or to an MCP client. The journal is where a tool like this leaks:
hostnames and machine ids in every record, command lines with secrets in their
arguments, tokens and passwords in message text, and megabytes of output no
client can hold. This file is for someone deciding whether to run it. Sections
1 to 10 each name one defence: the threat it answers in one sentence, the code
that implements it, the test that proves it, and the configuration a user can
change (most defences have none, and say so). "What journalmcp does not defend
against" lists the limits, and "Configuration" collects every switch in one
place. Every test named here runs in the default suite
(`uv run --frozen pytest -q`) against recorded fixtures; none reaches the real
systemd.

Readbacks quoted below were taken on systemd 259 (259.5-0ubuntu3.4) on
2026-10-03 and 2026-10-04.

## 1. Read-only

No operation changes any unit's state. Every argv the core builds is
`systemctl [--user] <verb> ...` or `journalctl [--user] <form> ...`, and the
first token after the optional `--user` comes from one fixed list.

- Threat: a bug or a crafted argument must not be able to turn a status reader
  into something that stops, starts, enables or edits a service.
- Configuration: none; the verb list is source code, not a setting.
- Code: `SYSTEMCTL_READ_VERBS` (`list-units`, `list-timers`, `show`, `cat`,
  `is-system-running`), `JOURNALCTL_READ_FORMS` (`--output=json`,
  `--list-boots`) and `READ_VERBS` in `src/journalmcp/core.py`. Every argv
  builder goes through `_command()`, which raises `InvalidArgument` for a verb
  not on the list; nothing else in the package names a verb.
- Tests: `tests/test_core.py`: `BUILDERS` builds the argv of all eight
  operations (both scopes, plus filtered forms);
  `test_the_builders_cover_all_eight_operations`,
  `test_every_argv_starts_with_a_read_verb` (systemctl argv carry a systemctl
  verb, journalctl argv a journalctl form) and
  `test_the_read_verb_list_is_the_fixed_one`.

## 2. argv only

No shell, no `shell=True`, no command built by string joining. Caller values
are list elements, so shell metacharacters in them are inert text.

- Threat: a caller-supplied string (a unit name, a grep pattern) must never be
  parsed by a shell and run as a command.
- Configuration: none.
- Code: `SubprocessRunner.run` in `src/journalmcp/runner.py` calls
  `subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
  errors="replace", timeout=timeout, check=False)`; it is the only place in
  the package that starts a process. An undecodable byte in systemd's output
  becomes U+FFFD rather than an exception that escapes the typed errors
  (`tests/test_runner.py::test_real_runner_decodes_utf8_and_replaces_undecodable_bytes`).
- Tests: `tests/test_hardening.py`:
  `test_hardening_the_real_runner_never_passes_shell`,
  `test_hardening_the_real_runner_is_the_only_caller_of_subprocess_run` (an AST
  walk over every module) and `test_hardening_no_module_names_a_shell_form`
  (`shell=True`, `os.system(`, `os.popen(`, `subprocess.getoutput`,
  `subprocess.getstatusoutput`, `subprocess.Popen`);
  `tests/test_runner.py::test_real_runner_passes_an_argv_list_with_no_shell`.
  The injection tests ("Argument-injection tests" under Decisions below) prove that no
  caller value changes the command.

## 3. Unit names are validated

A unit name must match systemd's own unit-name rules before it is used.

- Threat: a unit name that starts with `-` or carries shell or control
  characters must not reach a command line, where it could be read as an option
  or smuggle in a second argument.
- Configuration: none.
- Code: `validate_unit_name()` in `src/journalmcp/core.py`, called by
  `unit_status`, `unit_file` and `journal` (when `unit` is given) before any
  argv exists. Each failure raises `InvalidArgument` naming the rule.
- Tests: `tests/test_unitname.py` (selected by `-k unitname`): the eleven
  suffixes, one `@` accepted and a second rejected, 255 characters accepted and
  256 rejected, an empty name, a space, `/`, a leading `-`, a newline and a NUL
  rejected, a name with no suffix rejected, the error naming its rule, and
  `test_every_unit_taking_operation_validates_before_building_argv`. The three
  `test_unit_injection_in_*` groups in `tests/test_injection.py` exercise the
  validator through every unit-taking operation with hostile names.
- The rules are in "Unit names" under Decisions below.

## 4. Journal field allowlist

The core keeps only the journal keys on one list and drops every other key of
every record before anything is emitted. A caller can never name a field:
filtering goes only through the `journal` parameters (`unit`, `since`,
`until`, `priority`, `grep`, `lines`).

- Threat: a journal record carries the machine's hostname and machine id and
  the full command line of the logging process, so emitting every key would leak
  identifiers and secrets that no message-text filter sees.
- Configuration: none; the list is source code, and a caller cannot extend it.
- Code: `JOURNAL_FIELDS` and `parse_journal()` in `src/journalmcp/core.py`.
- Tests: `tests/test_journal.py` (selected by `-k allowlist`):
  `test_allowlist_is_exactly_the_recorded_keys`,
  `test_allowlist_drops_every_key_not_on_it` (a record carrying `_HOSTNAME`,
  `_MACHINE_ID` and `_CMDLINE` keeps none of them) and
  `test_allowlist_applies_to_the_emitted_result_too`.
- The list, each key's consumer and the exclusions are in "The allowlist"
  under Decisions below.

## 5. Output caps and the truncation marker

Output is capped by lines and by bytes, and a cut is always visible: the typed
result carries `truncated: true`, a cut value ends in `[TRUNCATED]`, and the
CLI's rendered text ends with a `[TRUNCATED]` line.

- Threat: a busy journal or one runaway value (a dumped buffer, a stack trace)
  must not flood a terminal or fill an MCP client's context.
- Configuration: the `lines` parameter (the `--lines` option on the CLI) sets how
  many entries to ask for, from 1 to 1000; `journal` defaults to 100 and
  `unit_status` to 20. The byte caps (4096 per value, 65536 per result) are
  constants in `src/journalmcp/core.py`; changing them means editing source.
- Code: `JOURNAL_LINES_MAX`, `DEFAULT_JOURNAL_LINES`, `DEFAULT_STATUS_LINES`,
  `VALUE_BYTES_MAX`, `RESULT_BYTES_MAX`, `TRUNCATED_MARKER`, `_cap_text()` and
  `_finish_entries()` in `src/journalmcp/core.py`; `_count_line()` and the
  `UnitFileResult` branch of `render_text()` in `src/journalmcp/cli.py`.
- Tests (selected by `-k truncat`): in `tests/test_journal.py`,
  `test_more_lines_than_the_cap_yields_exactly_the_cap_and_the_marker_truncated`,
  `test_one_oversized_value_is_cut_with_the_marker_truncated`,
  `test_a_cut_lands_on_a_utf8_boundary_truncated`,
  `test_a_result_over_the_byte_cap_drops_the_oldest_entries_truncated`,
  `test_output_under_both_caps_carries_no_marker_truncated`,
  `test_redaction_runs_before_the_cut_so_no_partial_secret_survives_truncated`,
  and over the synthetic recordings
  `test_truncated_overflow_recording_keeps_the_newest_five` and
  `test_truncated_oversized_recording_cuts_the_long_value`; in
  `tests/test_operations.py`, `test_unit_status_caps_an_oversized_property_truncated`,
  `test_unit_status_truncated_follows_the_journal_truncated` and
  `test_unit_file_is_byte_capped_truncated`; the rendered marker in
  `tests/test_cli.py::test_journal_text_ends_with_the_marker_when_truncated`.
- The numbers, the measurements behind them and the `truncated` semantics are
  in "Caps" under Decisions below.

## 6. Redaction

A redaction pass runs over every string the fronts emit from the journal (every
kept field, not only `MESSAGE`), from `unit_file` (a unit file can carry
`Environment=SECRET=...`) and from `unit_status`'s properties. It is on by
default, configurable without editing code, and keeps IP addresses.

- Threat: log text routinely contains tokens, bearer headers, passwords and
  private keys, which must not be handed to a terminal log or to a model.
- Configuration: `JOURNALMCP_REDACT` (`on` or `off`; unset means on) and
  `JOURNALMCP_REDACT_PATTERNS` (a file of extra regular expressions). Details
  in "Redaction configuration" under Decisions below.
- Code: `src/journalmcp/redaction.py` (`Redactor`, `redactor_from_env`); the
  core calls `redactor_from_env()` on every call that emits text, so a change
  to the environment takes effect on the next call.
- Classes, in the order they run, each replaced by a visible marker:
  PEM blocks (`-----BEGIN ...-----` through `-----END ...-----`, replaced whole;
  an unterminated block to the end of the text) as `[REDACTED:pem]`; bearer
  credentials (`Authorization: Bearer ...` and bare `Bearer <token>`, the word
  kept) as `[REDACTED:bearer]`; `password=` and `secret=` pairs
  (case-insensitive, `=` or `:`, quoted or not, the key kept) as
  `[REDACTED:password]` or `[REDACTED:secret]` (the key is any word ending in
  `password`, `passwd`, `passphrase` or `secret`, so `db_password=` counts;
  `pwd` is deliberately not a key, because sudo logs `PWD=<directory>` on every
  command and a working directory is not a secret; a `pwd=` credential is an
  extra pattern for the configuration); tokens (a token-like key such as
  `token`, `api_key`, `apikey`, `access_key`, `secret_key`, `auth_key` or
  `private_key` with a value, a token keyword followed by a long opaque string,
  and JWT-shaped `eyJ....` strings) as `[REDACTED:token]`; then any extra
  patterns as `[REDACTED:custom]`. Redacting already-redacted text changes
  nothing.
- Tests (selected by `-k redact` where the name carries it):
  `tests/test_redaction.py` holds a positive and a negative case per class
  (`test_tokens_are_redacted` / `test_ordinary_text_near_the_word_token_is_kept`,
  `test_bearer_headers_are_redacted` / `test_the_word_bearer_in_prose_is_kept`,
  `test_password_and_secret_pairs_are_redacted` /
  `test_the_words_password_and_secret_without_a_pair_are_kept`,
  `test_a_pem_block_is_replaced_whole` /
  `test_begin_and_dashes_in_prose_are_kept`), plus
  `test_a_working_directory_field_named_pwd_is_kept`,
  `test_a_ufw_line_keeps_its_addresses`, `test_ipv6_addresses_are_kept`,
  `test_cursor_and_boot_id_shaped_strings_are_kept`,
  `test_redaction_is_idempotent` and the configuration tests
  (`test_unset_means_on_and_off_means_off`,
  `test_an_unknown_switch_value_is_a_configuration_error`,
  `test_extra_patterns_come_from_the_named_file`,
  `test_a_bad_extra_pattern_names_its_line`,
  `test_a_missing_patterns_file_is_a_configuration_error`). Over the
  hand-written fixture of invented secrets:
  `tests/test_journal.py::test_redact_the_synthetic_secrets_recording` (every
  class redacted, the ufw addresses and an IPv6 address kept, no invented
  secret anywhere in the result, `_BOOT_ID` and `__CURSOR` untouched),
  `test_redact_patterns_file_adds_a_custom_class_to_the_recording`,
  `test_redact_off_leaves_the_recording_verbatim`, and in
  `tests/test_operations.py` `test_unit_status_redacts_the_synthetic_recording_redact`
  and `test_unit_file_redacts_the_synthetic_recording_redact`. Through the CLI:
  `tests/test_cli.py::test_redaction_reaches_the_cli_and_turns_off_through_the_environment_redact`.
- Redaction is pattern matching and misses secrets in shapes it does not know;
  see "What journalmcp does not defend against" below. The configuration
  mechanism is in "Redaction configuration" under Decisions below.

## 7. Every subprocess has a timeout

- Threat: a wedged systemd or journal must not be able to hang the caller (a
  terminal, or an MCP client waiting on a tool call) forever.
- Configuration: none from the CLI or the MCP tools. Each core function takes a
  `timeout` argument for callers that use the library directly; the default is
  `DEFAULT_TIMEOUT = 10.0` seconds in `src/journalmcp/core.py`.
- Code: `DEFAULT_TIMEOUT = 10.0` in `src/journalmcp/core.py`; every operation
  takes `timeout` and passes it to `Runner.run(argv, timeout)`. `core._run`
  turns `subprocess.TimeoutExpired` into `CommandTimeout` and a start failure
  into `CommandFailed`; `_system_state` does the same for
  `is-system-running`.
- Tests: `tests/test_core.py::test_a_timeout_becomes_a_typed_error`, and one
  per operation: `tests/test_journal.py::test_journal_passes_the_timeout_to_the_runner`,
  `tests/test_operations.py::test_unit_status_passes_the_timeout_to_both_calls`,
  `test_unit_file_passes_the_timeout_to_the_runner`,
  `test_boots_passes_the_timeout_to_the_runner` and
  `test_system_summary_passes_the_timeout_to_every_call` (all six calls).
- Reason for 10 seconds: the largest call, the system-scope `list-units --all`,
  measured 0.01 s; ten seconds is three orders of magnitude of headroom and
  still short enough that a wedged systemd cannot hang a caller.

## 8. No network calls

- Threat: a dependency or a later change must not be able to send journal
  contents, which are sensitive even after redaction, off the machine.
- Configuration: none.
- Code: every module imports only the standard library and itself, except
  `server.py`, the MCP front, which also imports two third-party packages: the
  `mcp` SDK and `pydantic` (the SDK's own validation library, used for the strict
  `lines` type). Nothing in the package opens a socket; `systemctl` and
  `journalctl` talk to local managers, and the MCP front speaks over stdin and
  stdout only (item 10).
- Tests: `tests/test_hardening.py`:
  `test_hardening_no_module_imports_a_network_library` (no `socket`, `urllib`,
  `http`, `requests`, `httpx`, `aiohttp`, `ssl` or `asyncio`, checked by an AST
  walk because the core spells `.socket` as a unit suffix) and
  `test_hardening_every_import_is_the_standard_library_or_this_package`, whose
  `ALLOWED_THIRD_PARTY` dict is the one place a module may be granted a
  third-party import (`{"server.py": {"mcp", "pydantic"}}`, nothing else);
  `test_hardening_only_the_server_imports_the_mcp_sdk`;
  `test_hardening_the_tree_has_the_expected_modules` makes a new module
  visible to these checks.
- The SDK is not network-free code: importing `journalmcp.server` loads
  `uvicorn`, `starlette`, `sse_starlette`, `socket` and `ssl` (read back from
  `sys.modules`), because the SDK carries HTTP transports. The guards for the
  MCP front are therefore about what runs, not what loads; they are in item 10.

## 9. No privilege escalation

The program runs as the invoking user: no `sudo`, no `pkexec`, no setuid
helper. The system journal is read with whatever access the invoking user
already has (for example, membership of the `adm` group).

- Threat: the program must not become a route to privileges the invoking user
  does not already hold.
- Configuration: none; there is no option to escalate. What the invoking user
  cannot read stays unreadable to journalmcp.
- Code: no argv builder can emit anything but `systemctl` or `journalctl` as
  its first token (`_command()` in `src/journalmcp/core.py`).
- Tests: `tests/test_hardening.py::test_hardening_no_argv_escalates_privilege`
  walks `BUILDERS` and asserts the first token is `systemctl` or `journalctl`
  and that no token is `sudo`, `pkexec`, `su` or `doas`.

## 10. The MCP wiring is a pinned `env -i` wrapper

The MCP client never starts the server through a bare package-runner launch,
which resolves and downloads packages on every start. It starts
`tools/journalmcp-mcp` (installed as `~/.local/bin/journalmcp-mcp`), which
runs the server from the checkout's locked environment and hands it an
environment built from nothing.

- Threat: a launch that resolves packages at start-up can run whatever the
  package index serves that day, and a server that inherits its client's
  environment inherits that environment's secrets (agent sockets, tokens).
- Configuration: `JOURNALMCP_REPO` names the checkout the wrapper runs from; the
  allowlist below is the wrapper's own source, so adding a variable means editing
  that file and is visible in review.
- Code: `tools/journalmcp-mcp` ends in
  `exec env -i "${allow[@]}" "$PYTHON" "$REPO/journalmcp" serve`, where `PYTHON`
  is `$REPO/.venv/bin/python`.
  The interpreter is the `.venv` that `uv sync --frozen` builds from
  `uv.lock` (`mcp==2.3.0` and its locked dependencies); starting an
  interpreter on a file resolves nothing and downloads nothing. The checkout
  is `$JOURNALMCP_REPO`, or a default directory under `$HOME`; the file names
  no user. It refuses, with one line on stderr and exit 1, a missing
  launcher, a missing `.venv` or an unset `XDG_RUNTIME_DIR`.
- The `env -i` allowlist, each entry with its reason:

| Variable | Value | Reason |
|---|---|---|
| `HOME` | the caller's | The SDK and the child processes expect one; it carries no secret. |
| `PATH` | `/usr/bin:/bin` | `systemctl` and `journalctl` are in `/usr/bin`; no user-writable directory is searched. |
| `XDG_RUNTIME_DIR` | the caller's | `systemctl --user` finds the user manager through it. Read back without it: `systemctl exited 1: Failed to connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined`. With it, `./journalmcp units --scope user --json` under the same `env -i` line returned `{"scope": "user", "units": [...]}` with 488 units. |
| `JOURNALMCP_REDACT` | forwarded only if set | The redaction switch (item 6); without it the MCP front cannot be configured. Only when set, because the core refuses an empty value rather than reading it as unset. Read back: `JOURNALMCP_REDACT=bogus` through the wrapper made a `unit_file` call return `JOURNALMCP_REDACT must be 'on' or 'off', not 'bogus'`, and `off` returned the file. |
| `JOURNALMCP_REDACT_PATTERNS` | forwarded only if set | The extra-patterns file (item 6), for the same reason. |

  Nothing else passes: no `SSH_AUTH_SOCK`, no `DBUS_SESSION_BUS_ADDRESS`, no
  tokens, no `LANG`. `LANG` was not needed: with no locale variable, Python
  3.12 runs in UTF-8 mode (read back under the wrapper's environment:
  `sys.flags.utf8_mode` 1, stdin and stdout `utf-8`), and the smoke test
  showed no encoding error. `TERM` is not needed by a stdio server.
- Smoke test: `tools/smoke-test-mcp tools/journalmcp-mcp` sends one real
  `initialize` request on stdin under `timeout 30` and passes only when
  the server exits 0, stdout holds `serverInfo` and stderr is empty (a server
  that answers and then hangs until `timeout` kills it fails). Read back:
  `"serverInfo":{"name":"journalmcp","version":"0.1.0"}`, server exit 0 when
  stdin closed, 0.66 s. The server is built with `log_level="WARNING"`, which
  keeps stderr empty on an anticipated tool error too (at the SDK's default
  `INFO`, one bad-scope call wrote 107 bytes to stderr).
- No listener. `server.py` calls `run(transport="stdio")` and passes no other
  transport; no host, port or HTTP server appears in it.
  `tests/test_hardening.py`: `test_hardening_the_server_runs_over_stdio_only`
  (the one `.run()` call has exactly the keyword `transport="stdio"`),
  `test_hardening_the_server_names_no_listener` (no name or string `sse`,
  `streamable-http`, `uvicorn`, `starlette`, `host` or `port` in the module),
  `test_hardening_the_cli_imports_the_server_only_inside_serve`, and
  `test_hardening_the_server_binds_listens_and_connects_nowhere`, which
  patches `socket.socket.bind`, `listen`, `connect` and `connect_ex` to fail
  and then calls all eight tools and the prompt through the in-memory client.
  Each was proved to bite: `transport="sse"` turned the first two red; a
  module-level `import mcp` in `cli.py` turned three red; a `connect()` added
  to one tool turned the runtime test red. A live readback of the running
  server (`ss` on its pid) listed no listening socket and no TCP or UDP
  socket; its only sockets were one unnamed AF_UNIX stream pair connected to
  each other, the shape of asyncio's self-pipe.
- The example config, `docs/examples/mcp.json`, points at the installed
  wrapper (`/home/YOUR-USER/.local/bin/journalmcp-mcp`), never at a package
  runner or at the launcher. No `.mcp.json` sits at the repo root.

## What journalmcp does not defend against

The ten defences above bound what the program itself can do. They do not
change who is running it or who is reading its output.

- **It reads what the invoking user can read.** journalmcp runs as the user who
  starts it and never escalates (item 9). Everything that user may read in the
  journal, in unit properties and in unit files is in scope, and the allowlist
  and redaction only narrow what is emitted, not what is read. A user in the
  `adm` or `systemd-journal` group gives the program the system journal; a user
  outside them gets their own. If you would not paste a log into a chat, check
  the redaction setting before you point a client at it.
- **It is not a sandbox.** The guarantees are properties of the code, enforced
  by tests, not by the operating system: the process has all of the invoking
  user's file and network rights, and there is no seccomp filter, namespace or
  container around it. Anyone who can edit the checkout, the wrapper or the
  locked environment, or who can substitute a dependency, can change every
  guarantee here. Items 1 to 9 hold for the code as shipped; they do not protect
  against that code being replaced.
- **The MCP client decides what to show the model.** The server returns
  redacted, capped, structured results to whichever client started it over
  stdio; there is no authentication on that channel, because the process
  boundary is the boundary. What the client then displays, stores, logs or
  sends on to a model is the client's choice. The tools declare themselves
  read-only and closed-world (`tests/test_inmemory.py::test_inmemory_every_tool_is_annotated_read_only_and_closed_world`),
  but that is a hint a client may ignore, not a control.
- **Journal text is untrusted input to a model.** Any local process, and any
  remote party who can make a service log a line, chooses text that journalmcp
  returns verbatim after redaction. A model that reads it may be steered by
  instructions embedded in it. journalmcp returns it as data and cannot stop a
  model from acting on it; a client should treat tool results as data too.
- **Redaction is pattern matching.** It catches the shapes listed in item 6 and
  misses a secret in any other shape: a password in a free-text sentence, a key
  in an unusual format, a value logged without a recognisable key. IP addresses
  are kept by default because a firewall log is useless without them. Add
  patterns for your own formats (`JOURNALMCP_REDACT_PATTERNS`); a `pwd=`
  credential, for instance, is not redacted by default. Known gaps in this
  version: each journal entry is redacted on its own, so a PEM block a service
  printed to stdout (which journald stores one line per entry) loses only its
  `BEGIN` line; a key name with two or more prefix words
  (`AWS_SECRET_ACCESS_KEY`), a quoted key (`"password": "..."`), URL userinfo
  (`scheme://user:pass@host`), `Basic` credentials and `--password <value>`
  flags are not matched.
- **The CLI's text output prints log text as written.** Control characters,
  terminal escape sequences included, and embedded newlines in a journal
  message, a unit description or a unit file reach the terminal unchanged, so
  whoever can write a journal entry can disturb what the text view shows. The
  `--json` output and the MCP front escape them; use `--json` when reading
  entries other users or services could have written.
- **The allowlist still emits message text, unit names and process ids.**
  That is the point of the tool; the fields it drops (item 4) are the ones with
  no reader.
- **The wrapper's checks are not in the pytest suite.** The `env -i` wrapper is
  verified by `tools/smoke-test-mcp`, run by hand, not by a test that runs on
  every change.

## Configuration

Every setting a user can change, in one place. There is no configuration file;
the environment is the one mechanism, so a shell and an MCP client's server
entry offer the same switches.

| Setting | Where | Effect |
|---|---|---|
| `JOURNALMCP_REDACT` | environment | `on` or unset keeps redaction on; `off` turns it off; any other value is a configuration error (item 6). |
| `JOURNALMCP_REDACT_PATTERNS` | environment | Path of a file with one extra regular expression per line; each match becomes `[REDACTED:custom]` (item 6). |
| `lines` / `--lines` | tool parameter, CLI option | How many entries to ask for, 1 to 1000; defaults 100 for `journal`, 20 for `unit_status` (item 5). |
| `JOURNALMCP_REPO` | environment of the wrapper | The checkout the wrapper runs the server from (item 10). |
| the wrapper's allowlist | `tools/journalmcp-mcp` | Which variables reach the server; adding one is an edit to that file (item 10). |
| byte caps, timeout, verb list, field allowlist | source constants in `src/journalmcp/core.py` | Changed only by editing source (items 1, 4, 5, 7). |

The MCP front can only see the two redaction variables if the wrapper forwards
them, which it does when they are set. Turning redaction off is a decision to
make once, in the place that starts the server, not per call: no tool
parameter can switch it off.

## Decisions

### The allowlist

A key is kept only for a named consumer:

| Key | Consumer |
|---|---|
| `MESSAGE` | the log line itself |
| `PRIORITY` | severity, for triage and the `priority` filter |
| `SYSLOG_IDENTIFIER` | which program logged (the prefix of the CLI's line) |
| `__REALTIME_TIMESTAMP` | when; rendered as UTC by the CLI, and the sort key |
| `__CURSOR` | a position to resume or de-duplicate a later query |
| `_SYSTEMD_UNIT` | which system unit the entry came from |
| `_SYSTEMD_USER_UNIT` | which user unit the entry came from |
| `UNIT`, `USER_UNIT` | the unit a manager message is about ("Started x") |
| `_PID` | tells one process instance from the next across restarts |
| `_COMM` | the executable, when `SYSLOG_IDENTIFIER` is absent |
| `_BOOT_ID` | which boot; joins with `boots()` |

Excluded, with the reason: `_HOSTNAME` and `_MACHINE_ID` identify the
machine; `_CMDLINE` can carry secrets in its arguments. Every other key (59
distinct keys appeared in the union over 500 real entries) has no consumer and
is dropped.

### Argument-injection tests

`tests/test_injection.py` (selected by `-k injection`) has one test per
caller-supplied parameter that reaches argv, across all eight operations:
`scope` (for each of the six scoped operations), `state`, `type`, `pattern`,
`unit` (for each of `unit_status`, `unit_file` and `journal`), `since`,
`until`, `priority`, `grep` and `lines`. Each runs five values:

| Id | Value |
|---|---|
| `leading-dash` | `--version` |
| `shell-metacharacters` | `; echo INJECTED; #` |
| `command-substitution` | `$(echo INJECTED)` |
| `newline` | `x\nINJECTED` |
| `nul` | `x\x00INJECTED` |

Each asserts one of two outcomes: the value is rejected with `InvalidArgument`
before any argv exists, or it lands as exactly one argv element, in the
`--flag=value` form or after `--`, appended to an otherwise unchanged argv.
`scope`, `state`, `type`, `unit`, `priority` and `lines` reject all five.
`pattern` and `grep` take the first three as one token (after `--`, and as
`--grep=<value>`) and reject the two with control characters. `since` and
`until` take `--version` as the one token `--since=--version` (journalctl
then fails to parse it as a time, which reaches the caller as
`CommandFailed`) and reject the other four through their charset.
`test_every_operation_with_a_parameter_has_an_injection_test_here` fails if a
parameter loses its test. Nothing is ever run: the metacharacters are inert
text in a list, and no destructive text is typed even as a string.

### Caps

Measured on 2026-10-03 on a development machine's journal:

| Measurement | Bytes |
|---|---|
| `journalctl -n 100 -o json \| wc -c` | 107959 |
| longest of those 100 lines | 2503 |
| the same 100 entries reduced to the allowlisted keys, compact JSON | 48486 |
| longest allowlisted entry | 1610 |
| longest `MESSAGE` | 1064 |

- `VALUE_BYTES_MAX = 4096` bytes per string value: more than twice the
  longest raw line, so no ordinary entry is cut, while one runaway value (a
  dumped buffer, a stack trace) cannot fill the result.
- `RESULT_BYTES_MAX = 65536` bytes per result, measured on the compact JSON of
  the entries list (and on the whole text for `unit_file`): it holds the
  default 100 allowlisted entries (48486 bytes) with room to spare, while
  staying a bounded share of an MCP client's context.
- Line caps: `lines` is an integer from 1 to `JOURNAL_LINES_MAX = 1000`;
  `journal` defaults to `DEFAULT_JOURNAL_LINES = 100` and `unit_status` to
  `DEFAULT_STATUS_LINES = 20`.
- The marker is `[TRUNCATED]`. A value cut to the cap ends in it (the marker
  counts toward the 4096 bytes, and the cut lands on a UTF-8 character
  boundary); rendered text ends in a `[TRUNCATED]` line.
- `truncated` is true only when the core itself cut entries or text. The
  `lines` parameter bounds what journalctl returns and is not a truncation; if
  journalctl returns more entries than `lines` anyway, the core keeps the
  newest `lines` and sets `truncated`.
- When the entries list exceeds the byte cap, the oldest entries are dropped
  first, so the result always ends at the newest entry.
- Redaction runs before any cut, so a cut can never leave part of a secret.

### Redaction configuration

One mechanism, the environment, read by the core at call time so both fronts
share it:

- `JOURNALMCP_REDACT`: `on` or unset keeps redaction on; `off` turns it off.
  Any other value raises `ConfigurationError`, so a typo such as `of` or
  `false` cannot pass silently as either setting. The check fails closed.
- `JOURNALMCP_REDACT_PATTERNS`: the path of a file with one regular expression
  per line (blank lines and `#` comments ignored); each match becomes
  `[REDACTED:custom]`. An unreadable file or an invalid pattern raises
  `ConfigurationError` naming the line.

Chosen over a config file under `XDG_CONFIG_HOME` because an environment
variable is what both a shell and an MCP client's server configuration
already offer; nothing is parsed from the home directory; and the wrapper's
`env -i` allowlist must name the two variables to forward them, which keeps
the switch visible in the one file that defines the server's environment.

### Unit names

From `man systemd.unit`:

> Valid unit names consist of a "unit name prefix", and a suffix specifying
> the unit type which begins with a dot. The "unit name prefix" must consist
> of one or more valid characters (ASCII letters, digits, ":", "-", "_", ".",
> and "\"). The total length of the unit name including the suffix must not
> exceed 255 characters. The unit type suffix must be one of ".service",
> ".socket", ".device", ".mount", ".automount", ".swap", ".target", ".path",
> ".timer", ".slice", or ".scope".

> A template unit must have a single "@" at the end of the unit name prefix
> (right before the type suffix).

`validate_unit_name()` enforces those rules, with at most one `@` separating
a template prefix from its instance string (`gnome-session@ubuntu.target` is
accepted; `a@b@c.service` is rejected). It is stricter than systemd in one
place, on purpose: a name may not begin with `-`, so no unit name can ever be
read as an option by the command that receives it. A template name with an
empty instance (`getty@.service`) is accepted: it is a valid name to ask
about, and `systemctl cat getty@.service` prints the template file.
