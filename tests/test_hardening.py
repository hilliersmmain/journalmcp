"""Hardening: no network, no shell, no escalation, read from the source tree.

The import check walks the AST rather than grepping text, because the core
spells `.socket` as a unit type suffix. A later module that must import a
third-party package is named in ALLOWED_THIRD_PARTY, and nowhere else: today
that is `server.py`, the MCP front, the one module allowed to import `mcp`
(and `pydantic`, the SDK's own validation library, for strict parameter types).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from test_core import BUILDERS

SRC = Path(__file__).resolve().parent.parent / "src" / "journalmcp"
MODULES = sorted(SRC.glob("*.py"))

NETWORK = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ssl", "asyncio"}
ESCALATION = {"sudo", "pkexec", "su", "doas"}
# Third-party roots a named module may import: the MCP front and its SDK, and
# no other module and no other library.
ALLOWED_THIRD_PARTY: dict[str, set[str]] = {"server.py": {"mcp", "pydantic"}}
# Names that would serve over something other than stdio, or listen at all.
LISTENER_FORMS = ("sse", "streamable-http", "streamable_http", "uvicorn", "starlette", "host", "port")
# Text forms that would start a shell or a second process path. These are
# strings the test asserts are absent from the source; nothing here calls them.
SHELL_FORMS = ("shell=True", "os.system(", "os.popen(", "subprocess.getoutput", "subprocess.getstatusoutput", "subprocess.Popen")


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_hardening_the_tree_has_the_expected_modules() -> None:
    assert [m.name for m in MODULES] == [
        "__init__.py", "cli.py", "core.py", "errors.py", "redaction.py", "runner.py", "server.py",
    ]


def test_hardening_only_the_server_imports_the_mcp_sdk() -> None:
    importers = sorted(m.name for m in MODULES if "mcp" in imported_roots(m))
    assert importers == ["server.py"]


def test_hardening_the_cli_imports_the_server_only_inside_serve() -> None:
    # Module-level imports of cli.py stay standard library plus the core, so
    # `--version` and the eight query subcommands never load the SDK.
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    top_level = {
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    } | {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert not any(name.startswith(("mcp", "journalmcp.server")) for name in top_level)
    lazy = [
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef)
        for node in ast.walk(func)
        if isinstance(node, ast.ImportFrom) and node.module == "journalmcp"
        and any(alias.name == "server" for alias in node.names)
    ]
    assert lazy == ["_serve"]


def test_hardening_the_server_runs_over_stdio_only() -> None:
    # The one `.run(...)` call in the MCP front passes transport="stdio" and
    # nothing else, so no host, port or listening socket can exist.
    tree = ast.parse((SRC / "server.py").read_text(encoding="utf-8"))
    runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
    ]
    assert len(runs) == 1
    (call,) = runs
    assert call.args == []
    assert [(k.arg, ast.literal_eval(k.value)) for k in call.keywords] == [("transport", "stdio")]


def test_hardening_the_server_names_no_listener() -> None:
    names = {
        node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else node.arg
        for node in ast.walk(ast.parse((SRC / "server.py").read_text(encoding="utf-8")))
        if isinstance(node, (ast.Name, ast.Attribute, ast.keyword))
    }
    strings = {
        node.value
        for node in ast.walk(ast.parse((SRC / "server.py").read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for form in LISTENER_FORMS:
        assert form not in names, form
        assert form not in strings, form


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.name)
def test_hardening_no_module_imports_a_network_library(module: Path) -> None:
    assert not imported_roots(module) & NETWORK


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.name)
def test_hardening_every_import_is_the_standard_library_or_this_package(module: Path) -> None:
    allowed = set(sys.stdlib_module_names) | {"journalmcp"} | ALLOWED_THIRD_PARTY.get(module.name, set())
    assert imported_roots(module) <= allowed


@pytest.mark.parametrize("build", [b for _, b in BUILDERS], ids=[n for n, _ in BUILDERS])
def test_hardening_no_argv_escalates_privilege(build: object) -> None:
    argv = build()  # type: ignore[operator]
    assert argv[0] in ("systemctl", "journalctl")
    assert not set(argv) & ESCALATION


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.name)
def test_hardening_no_module_names_a_shell_form(module: Path) -> None:
    text = module.read_text(encoding="utf-8")
    for form in SHELL_FORMS:
        assert form not in text, form


def test_hardening_the_real_runner_is_the_only_caller_of_subprocess_run() -> None:
    callers = []
    for module in MODULES:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                callers.append((module.name, node.func.attr))
    assert callers == [("runner.py", "run")]


def test_hardening_the_real_runner_never_passes_shell() -> None:
    source = (SRC / "runner.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "shell" not in source.split("subprocess.run(")[1].split(")")[0]


@pytest.mark.anyio
async def test_hardening_the_server_binds_listens_and_connects_nowhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Importing the SDK loads its HTTP transport modules, so the import check
    # above is not the whole proof: at run time, with every socket that would
    # bind, listen or connect made to fail, the server answers every tool and
    # the prompt. (asyncio's own self-pipe socketpair calls none of the three.)
    import socket

    from conftest import FIXTURES
    from mcp import Client

    from journalmcp.runner import FixtureRunner
    from journalmcp.server import build_server

    attempts: list[str] = []

    def refuse(name: str) -> object:
        def method(self: socket.socket, *args: object, **kwargs: object) -> None:
            attempts.append(name)
            raise OSError(f"GUARD: the MCP front called socket.{name}")

        return method

    for name in ("bind", "listen", "connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse(name))

    calls = [
        ("list_units", {"scope": "user"}),
        ("failed_units", {"scope": "system"}),
        ("list_timers", {"scope": "user"}),
        ("unit_status", {"unit": "cron.service", "scope": "system"}),
        ("journal", {"scope": "system", "unit": "cron.service", "lines": 20}),
        ("boots", {}),
        ("unit_file", {"unit": "cron.service", "scope": "system"}),
        ("system_summary", {}),
    ]
    async with Client(build_server(FixtureRunner(FIXTURES))) as client:
        for tool, arguments in calls:
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, (tool, result.content)
        await client.get_prompt("triage_failed_unit", {"unit": "cron.service", "scope": "system"})
    assert attempts == []
