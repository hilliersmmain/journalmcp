"""Redaction: one function over text, on by default, configured through the
environment alone. Every class has a positive case (redacted) and a negative
case (left alone). IP addresses are never touched: ufw log lines need them.

Every secret below is an invented string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from journalmcp import redaction
from journalmcp.errors import ConfigurationError
from journalmcp.redaction import (
    PATTERNS_ENV,
    REDACT_ENV,
    Redactor,
    marker,
    redactor_from_env,
)

R = Redactor()

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n"
    "KUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQJAIJLixBy2qpFoS4DSmoEm\n"
    "-----END RSA PRIVATE KEY-----"
)
UFW = (
    "[UFW BLOCK] IN=wlan0 OUT= MAC=02:00:00:00:12:34:02:00:00:00:56:78:08:00 "
    "SRC=192.0.2.7 DST=198.51.100.3 LEN=60 TOS=0x00 PREC=0x00 TTL=64 ID=0 DF "
    "PROTO=TCP SPT=51234 DPT=22 WINDOW=64240 RES=0x00 SYN URGP=0"
)


def test_marker_is_visible_and_names_the_class() -> None:
    assert marker("token") == "[REDACTED:token]"


# --- tokens -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("api_key=abcdefghijklmnop1234", "api_key=[REDACTED:token]"),
        ("apikey: ZYXWVUTSRQPONMLKJIHG", "apikey: [REDACTED:token]"),
        ('access_key="AKIAEXAMPLE0123456789"', 'access_key="[REDACTED:token]"'),
        ("access_token=ya29.a0AfH6SMBexample-opaque_value", "access_token=[REDACTED:token]"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "token [REDACTED:token]"),
        (f"session {JWT} expired", "session [REDACTED:token] expired"),
    ],
)
def test_tokens_are_redacted(text: str, expected: str) -> None:
    assert R.redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "token refresh scheduled in 30 minutes",
        "api_key rotation completed",
        "fetched pkg-1.2.3.tar.gz from the mirror",
        "Activating service name='org.gnome.Nautilus' requested by ':1.24'",
        "the token bucket is full",
    ],
)
def test_ordinary_text_near_the_word_token_is_kept(text: str) -> None:
    assert R.redact(text) == text


# --- bearer headers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def-1234567890", "Authorization: Bearer [REDACTED:bearer]"),
        ("got bearer xyz12345678 from the client", "got bearer [REDACTED:bearer] from the client"),
        (f"Authorization: Bearer {JWT}", "Authorization: Bearer [REDACTED:bearer]"),
    ],
)
def test_bearer_headers_are_redacted(text: str, expected: str) -> None:
    assert R.redact(text) == expected


def test_the_word_bearer_in_prose_is_kept() -> None:
    text = "the bearer of bad news was shown the door"
    assert R.redact(text) == text


# --- password= and secret= pairs ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password=hunter2", "password=[REDACTED:password]"),
        ('PASSWORD: "hunter two"', "PASSWORD: [REDACTED:password]"),
        ("Password = 'hunter2'", "Password = [REDACTED:password]"),
        ("secret=s3cr3t-value", "secret=[REDACTED:secret]"),
        ("Environment=SECRET=abc123 Environment=PASSWD=def", "Environment=SECRET=[REDACTED:secret] Environment=PASSWD=[REDACTED:password]"),
        ("client_secret: zzz", "client_secret: [REDACTED:secret]"),
    ],
)
def test_password_and_secret_pairs_are_redacted(text: str, expected: str) -> None:
    assert R.redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "exampleuser : TTY=pts/0 ; PWD=/home/exampleuser ; USER=root ; COMMAND=/usr/bin/apt update",
        "pwd=/srv/app cwd=/srv/app",
    ],
)
def test_a_working_directory_field_named_pwd_is_kept(text: str) -> None:
    # sudo logs PWD= on every command line; it is a path, never a secret. A real
    # `pwd=` credential is a custom pattern for the configuration, not a default class.
    assert R.redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "password changed for user bob",
        "secret santa draw complete",
        "Passwords must be at least 12 characters",
    ],
)
def test_the_words_password_and_secret_without_a_pair_are_kept(text: str) -> None:
    assert R.redact(text) == text


# --- PEM blocks ------------------------------------------------------------------


def test_a_pem_block_is_replaced_whole() -> None:
    text = f"loaded key\n{PEM}\ndone"
    assert R.redact(text) == "loaded key\n[REDACTED:pem]\ndone"
    assert "MIIBOg" not in R.redact(text)


def test_an_unterminated_pem_block_is_redacted_to_the_end() -> None:
    head = PEM.split("\n-----END")[0]
    assert R.redact(f"partial {head}") == "partial [REDACTED:pem]"


@pytest.mark.parametrize("text", ["BEGIN transaction", "----- separator -----", "END OF FILE"])
def test_begin_and_dashes_in_prose_are_kept(text: str) -> None:
    assert R.redact(text) == text


# --- what is kept on purpose -------------------------------------------------------


def test_a_ufw_line_keeps_its_addresses() -> None:
    assert R.redact(UFW) == UFW
    assert "SRC=192.0.2.7" in R.redact(UFW) and "DST=198.51.100.3" in R.redact(UFW)


def test_ipv6_addresses_are_kept() -> None:
    text = "connection from 2001:db8::7 closed, 2001:db8:0:1::2 retried"
    assert R.redact(text) == text


def test_cursor_and_boot_id_shaped_strings_are_kept() -> None:
    cursor = "s=0123456789abcdef0123456789abcdef;i=1a2b;b=0123456789abcdef0123456789abcdef;m=1234abcd;t=5f1a2b3c4d5e6;x=abcdef0123456789"
    assert R.redact(cursor) == cursor
    assert R.redact("0123456789abcdef0123456789abcdef") == "0123456789abcdef0123456789abcdef"


def test_redaction_is_idempotent() -> None:
    once = R.redact(f"password=hunter2 {PEM} Bearer abcdefgh12 {JWT}")
    assert R.redact(once) == once


# --- configuration: the environment, read by the core for both fronts -------------


def test_off_leaves_text_alone() -> None:
    assert Redactor(enabled=False).redact("password=hunter2") == "password=hunter2"


def test_unset_means_on_and_off_means_off() -> None:
    assert redactor_from_env({}).enabled is True
    assert redactor_from_env({REDACT_ENV: "on"}).enabled is True
    assert redactor_from_env({REDACT_ENV: "off"}).enabled is False
    assert redactor_from_env({REDACT_ENV: "off"}).redact("password=x") == "password=x"


def test_an_unknown_switch_value_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match=REDACT_ENV):
        redactor_from_env({REDACT_ENV: "maybe"})


def test_extra_patterns_come_from_the_named_file(tmp_path: Path) -> None:
    patterns = tmp_path / "extra.txt"
    patterns.write_text("# order numbers\n\ncafe-\\d+\n^ticket [A-Z]{3}-\\d+$\n")
    r = redactor_from_env({PATTERNS_ENV: str(patterns)})
    assert r.redact("order cafe-42 shipped") == "order [REDACTED:custom] shipped"
    assert r.redact("ticket ABC-7") == "[REDACTED:custom]"
    assert r.redact("ticket ABC-7 reopened") == "ticket ABC-7 reopened"
    assert r.redact("password=hunter2") == "password=[REDACTED:password]"


def test_a_bad_extra_pattern_names_its_line(tmp_path: Path) -> None:
    patterns = tmp_path / "extra.txt"
    patterns.write_text("fine\n(unclosed\n")
    with pytest.raises(ConfigurationError, match="line 2"):
        redactor_from_env({PATTERNS_ENV: str(patterns)})


def test_a_missing_patterns_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=PATTERNS_ENV):
        redactor_from_env({PATTERNS_ENV: str(tmp_path / "absent.txt")})


def test_the_default_redactor_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REDACT_ENV, "off")
    assert redaction.redactor_from_env().enabled is False
    monkeypatch.delenv(REDACT_ENV)
    assert redaction.redactor_from_env().enabled is True
