"""The fixture scrub in tools/capture-fixtures, fed a crafted record.

The live hostname and username are read at test time and appear in no file.
The gate term is read from tools/public-gate.sh at test time for the same
reason: this file is export-bound and may not spell one. The gate and the
private scrub list are not in the public export, so the tests that need them
skip there; a never-matching pattern stands in for the gate in the rest.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import re
import socket
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest
from conftest import fixture_index

TOOL = Path(__file__).resolve().parent.parent / "tools" / "capture-fixtures"


def _load_tool() -> ModuleType:
    loader = SourceFileLoader("capture_fixtures", str(TOOL))
    spec = importlib.util.spec_from_file_location("capture_fixtures", TOOL, loader=loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["capture_fixtures"] = module
    loader.exec_module(module)
    return module


cf = _load_tool()
HAS_GATE = cf.GATE.exists()
needs_gate = pytest.mark.skipif(not HAS_GATE, reason="tools/public-gate.sh is not in the public export")
GATE = cf.read_gate_pattern() if HAS_GATE else re.compile("(?!)")
LIVE = cf.read_live()
NO_GATE = re.compile("(?!)")


def _literal_terms() -> list[str]:
    return [t for t in GATE.pattern.split("|") if re.fullmatch(r"[a-z][a-z-]+", t)]


def _ruled_term() -> str:
    return next(t for t in _literal_terms() if cf.term_key(t) in cf.TERM_RULES)


def _unruled_term() -> str:
    return next(t for t in _literal_terms() if cf.term_key(t) not in cf.TERM_RULES)


def _crafted() -> str:
    return json.dumps(
        {
            # An earlier name: the journal keeps entries from before a rename,
            # which the live-hostname rule cannot know about.
            "_HOSTNAME": "old-box-name",
            "host": LIVE.hostname,
            "_MACHINE_ID": "ab" * 16,
            "user": LIVE.username,
            "mac": "3c:22:fb:12:34:56",
            "v4": "10.20.30.40",
            "v6": "fe80::1c2b:3aff:fe4d:5e6f",
            "unit": f"{_ruled_term().upper()}.service",
            "home": f"/home/{LIVE.username}/x",
        },
        separators=(",", ":"),
    )


@needs_gate
def test_nothing_identifying_survives() -> None:
    text, counts = cf.scrub(_crafted(), LIVE, GATE)
    lowered = text.lower()
    assert LIVE.hostname.lower() not in lowered
    assert LIVE.username.lower() not in lowered
    for real in ("3c:22:fb:12:34:56", "10.20.30.40", "fe80::1c2b:3aff:fe4d:5e6f", "ab" * 16):
        assert real not in lowered
    assert "old-box-name" not in text
    assert not GATE.search(text)
    for label in ("hostname", "username", "mac", "ipv4", "ipv6", "_HOSTNAME", "_MACHINE_ID", "gate-term"):
        assert counts[label] >= 1, label
    cf.assert_clean(text, LIVE, GATE)


@needs_gate
def test_placeholders_come_from_the_documentation_ranges() -> None:
    text, _ = cf.scrub(_crafted(), LIVE, GATE)
    assert re.search(r'"mac":"02:00:00:00:[0-9a-f]{2}:[0-9a-f]{2}"', text)
    assert re.search(r'"v4":"(192\.0\.2|198\.51\.100|203\.0\.113)\.\d+"', text)
    assert re.search(r'"v6":"2001:db8::[0-9a-f]+"', text)
    assert f'"_HOSTNAME":"{cf.HOST_PLACEHOLDER}"' in text
    assert f'"_MACHINE_ID":"{cf.MACHINE_ID_PLACEHOLDER}"' in text


@needs_gate
def test_two_runs_give_the_same_placeholders() -> None:
    assert cf.scrub(_crafted(), LIVE, GATE) == cf.scrub(_crafted(), LIVE, GATE)
    assert cf.fake_ipv4("10.20.30.40") == cf.fake_ipv4("10.20.30.40")
    assert cf.fake_mac("3c:22:fb:12:34:56") == cf.fake_mac("3C:22:FB:12:34:56")


def test_the_systemd_escaped_hostname_is_scrubbed() -> None:
    live = cf.Live("host-with-dash", "nobodyuser", "")
    text, _ = cf.scrub('{"unit":"run-host\\\\x2dwith\\\\x2ddash.mount"}', live, GATE)
    assert "with" not in text
    assert "example\\\\x2dhost" in text


@needs_gate
def test_a_gate_term_with_no_rule_refuses_the_fixture() -> None:
    term = _unruled_term()
    with pytest.raises(cf.ScrubRefused) as caught:
        cf.scrub(f'{{"description":"uses {term} daily"}}', LIVE, GATE)
    assert caught.value.hits == [term]


@pytest.mark.parametrize("secret", ["Authorization: Bearer abc", "PASSWORD=hunter2", "x token=1", "-----BEGIN RSA PRIVATE KEY-----"])
def test_a_secret_shape_refuses_the_fixture(secret: str) -> None:
    with pytest.raises(cf.ScrubRefused, match="secret shape"):
        cf.scrub(secret, LIVE, GATE)


def test_loopback_and_pci_addresses_are_left_alone() -> None:
    text = '"127.0.0.1 ::1 0000:00:1f.3 252:1-25166331 12:34:56"'
    assert cf.scrub(text, LIVE, GATE)[0] == text


def test_session_numbers_lose_their_digit_after_the_word() -> None:
    word = "Ses" + "sion"
    text, _ = cf.scrub(f'"{word} 7 of User x"', LIVE, GATE)
    assert text == f'"{word} example-7 of User x"'
    assert not GATE.search(text)


def test_the_last_pass_catches_a_live_value() -> None:
    with pytest.raises(cf.ScrubRefused):
        cf.assert_clean(f"left over: {socket.gethostname()}", LIVE, GATE)
    with pytest.raises(cf.ScrubRefused):
        cf.assert_clean(f"left over: {getpass.getuser()}", LIVE, GATE)


def test_the_tool_only_records_read_commands() -> None:
    with pytest.raises(SystemExit):
        cf.check_argv(["systemctl", "--user", "stop", "x.service"])
    with pytest.raises(SystemExit):
        cf.check_argv(["bash", "-c", "true"])
    cf.check_argv(["systemctl", "--user", "list-units", "--output=json"])


@pytest.mark.parametrize(
    "argv",
    [
        ["journalctl", "--vacuum-time=1s"],
        ["journalctl", "--output=json", "--vacuum-time=1s"],
        ["journalctl", "--rotate"],
        ["journalctl", "--flush"],
        ["journalctl", "--user", "--output=json", "--no-pager", "--lines=5", "--machine=evil"],
        ["journalctl", "--list-boots", "--output=json", "-H", "evil"],
        ["systemctl", "--host=evil", "list-units", "--output=json"],
        ["systemctl", "-H", "evil", "list-units", "--output=json"],
        ["systemctl", "list-units", "--output=json", "--no-block"],
    ],
    ids=lambda a: " ".join(a),
)
def test_the_tool_refuses_journal_mutators_remote_hosts_and_unknown_options(argv: list[str]) -> None:
    # journalctl has no verb: its mutators are options, so the check must read the
    # options too, and only the ones the core itself emits may pass.
    with pytest.raises(SystemExit):
        cf.check_argv(argv)


def test_every_recorded_argv_passes_the_tool_check() -> None:
    for entry in fixture_index():
        cf.check_argv(entry["argv"])


def _recorded() -> list[tuple[str, str]]:
    """(file name, text) for every recorded fixture; synthetic ones are hand-written."""
    names = sorted({e["file"] for e in fixture_index() if not e["file"].startswith("synthetic-")})
    return [(n, (cf.FIXTURES / n).read_text(encoding="utf-8")) for n in names]


def unscrubbed_disk_ids(texts: list[tuple[str, str]]) -> list[str]:
    return [
        f"{name}: {m.group('id')}"
        for name, text in texts
        for rule in cf._DISK_IDS
        for m in rule.finditer(text)
        if cf.fake_disk_id(m.group("id")) != m.group("id")
    ]


def surviving_private_literals(texts: list[tuple[str, str]], rules: list[tuple[str, str]]) -> list[str]:
    hits = []
    for name, text in texts:
        try:
            cf.assert_clean(text, cf.Live("", "", ""), NO_GATE, rules)
        except cf.ScrubRefused:
            hits.append(name)
    return hits


def test_disk_ids_become_placeholders_of_the_same_shape() -> None:
    uuid = "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    raw = uuid.replace("-", "")
    text = json.dumps(
        {
            "unit": "dev-disk-by\\x2duuid-" + uuid.replace("-", "\\x2d") + ".device",
            "description": f"/dev/disk/by-uuid/{uuid}",
            "luks": f"/dev/disk/by-id/dm-uuid-CRYPT-LUKS2-{raw}-dm_crypt-0",
            "lvm": "dm-uuid-LVM-" + "aB3" * 21 + "x",
            "pv": "lvm-pv-uuid-aB3dE5-fG7h-iJ9k-Lm1n-oP3q-rS5t-uV7wX9",
            "eui": "nvme-eui.0123456789abcdef0123456789abcdef",
            "fat": "/dev/disk/by-uuid/A1B2-C3D4",
        }
    )
    out, counts = cf.scrub(text, cf.Live("", "", ""), NO_GATE, [])
    assert counts["disk-id"] == 7
    for real in (uuid[:8], raw[:8], "aB3aB3", "aB3dE5", "0123456789abcdef0123", "A1B2"):
        assert real not in out
    fake = cf.fake_disk_id(uuid)
    assert re.fullmatch(r"00000000-0000-0000-0000-[0-9a-f]{12}", fake)
    assert f"/dev/disk/by-uuid/{fake}" in out
    assert fake.replace("-", "\\\\x2d") in out
    assert "LUKS2-" + fake.replace("-", "") + "-dm_crypt" in out
    assert re.search(r'"fat": "/dev/disk/by-uuid/0000-[0-9A-F]{4}"', out)
    assert cf.scrub(out, cf.Live("", "", ""), NO_GATE, [])[0] == out


def test_a_private_literal_is_replaced_even_after_a_systemd_escape() -> None:
    rules = [("Acme Widget Daemon", "Example service"), ("acmewidget", "example-widget")]
    text = '"nvme\\\\x2dACMEWIDGET_1 acmewidget.service Acme Widget Daemon notacmewidget"'
    out, counts = cf.scrub(text, cf.Live("", "", ""), NO_GATE, rules)
    assert out == '"nvme\\\\x2dexample-widget_1 example-widget.service Example service notacmewidget"'
    assert counts["private"] == 3
    with pytest.raises(cf.ScrubRefused, match="private literal"):
        cf.assert_clean(text, cf.Live("", "", ""), NO_GATE, rules)


def test_every_disk_id_in_the_recorded_fixtures_is_a_placeholder() -> None:
    assert unscrubbed_disk_ids(_recorded()) == []


@pytest.mark.skipif(not cf.PRIVATE.exists(), reason="the private scrub list is not in the public export")
def test_no_private_literal_survives_in_the_recorded_fixtures() -> None:
    assert cf.PRIVATE_RULES
    assert surviving_private_literals(_recorded(), cf.PRIVATE_RULES) == []


@needs_gate
def test_rescrubbing_the_recorded_fixtures_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name in [cf.INDEX_NAME] + [n for n, _ in _recorded()]:
        (tmp_path / name).write_bytes((cf.FIXTURES / name).read_bytes())
    assert cf.rescrub(tmp_path, dry_run=True) == 0
    assert "rewrite" not in capsys.readouterr().out
