"""Redaction: one function over text, applied to every string the fronts emit
from the journal, from unit files and from unit properties.

Classes, in the order they run: PEM blocks (replaced whole, an unterminated
block to the end of the text), bearer headers, `password=` and `secret=`
pairs, tokens (a token-like key with a value, a token keyword followed by a
long opaque string, and JWT-shaped strings), then any extra patterns from the
configuration. Each match becomes a visible marker, `[REDACTED:<class>]`, and
redacting already-redacted text changes nothing.

IP addresses are kept on purpose: ufw log lines are useless without them.

Configuration is the environment, read by the core for both fronts:

- `JOURNALMCP_REDACT=off` turns redaction off (`on`, or unset, keeps it on;
  any other value is a configuration error, so a typo cannot pass as "on").
- `JOURNALMCP_REDACT_PATTERNS=<file>` adds one regular expression per line
  (blank lines and `#` comments ignored); each match becomes
  `[REDACTED:custom]`. A line that is not a valid pattern is a configuration
  error naming its line number.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from journalmcp.errors import ConfigurationError

REDACT_ENV = "JOURNALMCP_REDACT"
PATTERNS_ENV = "JOURNALMCP_REDACT_PATTERNS"


def marker(cls: str) -> str:
    return f"[REDACTED:{cls}]"


# The characters a token, key or bearer credential is made of.
_OPAQUE = r"[A-Za-z0-9_\-./+=]"
# Key names that introduce a token; `access_token`, `api-key`, `apikey` and
# the like. A single word in front joined by `_` or `-` is part of the key.
_TOKEN_KEY = (
    r"(?:[a-z]+[_-])?"
    r"(?:token|api[_-]?key|apikey|access[_-]?key|secret[_-]?key|auth[_-]?key|private[_-]?key)"
)

_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----(?:[\s\S]*?-----END [A-Z0-9 ]+-----|[\s\S]*\Z)")
_BEARER = re.compile(r"\b(bearer)(\s+)" + _OPAQUE + r"{8,}", re.IGNORECASE)
# `pwd` is not a password key here: sudo logs `PWD=<directory>` on every
# command line, and a working directory is not a secret. A `pwd=` credential
# is an extra pattern for the configuration.
_PAIR = re.compile(
    r"\b([a-z_]*(?:password|passwd|passphrase|secret))\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_TOKEN_PAIR = re.compile(
    r"\b(" + _TOKEN_KEY + r")\b(\s*[=:]\s*)([\"']?)" + _OPAQUE + r"{8,}", re.IGNORECASE
)
_TOKEN_WORD = re.compile(r"\b(" + _TOKEN_KEY + r")(\s+)([\"']?)" + _OPAQUE + r"{20,}", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def _pair_class(match: re.Match[str]) -> str:
    return "secret" if match.group(1).lower().endswith("secret") else "password"


def _keep_groups(cls: str, *groups: int) -> Callable[[re.Match[str]], str]:
    """Keep the named groups (the key, the separator, an opening quote) and
    replace the rest of the match with the marker."""

    def replace(match: re.Match[str]) -> str:
        return "".join(match.group(g) for g in groups) + marker(cls)

    return replace


def _pair_replace(match: re.Match[str]) -> str:
    return match.group(1) + match.group(2) + marker(_pair_class(match))


_CLASSES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (_PEM, lambda m: marker("pem")),
    (_BEARER, _keep_groups("bearer", 1, 2)),
    (_PAIR, _pair_replace),
    (_TOKEN_PAIR, _keep_groups("token", 1, 2, 3)),
    (_TOKEN_WORD, _keep_groups("token", 1, 2, 3)),
    (_JWT, lambda m: marker("token")),
)


@dataclass(frozen=True)
class Redactor:
    """The redaction pass: on by default, with optional extra patterns."""

    enabled: bool = True
    extra: tuple[re.Pattern[str], ...] = ()

    def redact(self, text: str) -> str:
        if not self.enabled:
            return text
        for pattern, replace in _CLASSES:
            text = pattern.sub(replace, text)
        for pattern in self.extra:
            text = pattern.sub(marker("custom"), text)
        return text


def redactor_from_env(environ: Mapping[str, str] | None = None) -> Redactor:
    """Build the redactor the environment asks for; the process environment by default."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    switch = env.get(REDACT_ENV, "on").strip().lower()
    if switch not in ("on", "off"):
        raise ConfigurationError(f"{REDACT_ENV} must be 'on' or 'off', not {env[REDACT_ENV]!r}")
    extra: list[re.Pattern[str]] = []
    path = env.get(PATTERNS_ENV, "")
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"{PATTERNS_ENV}: cannot read {path!r}: {exc}") from exc
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                extra.append(re.compile(line))
            except re.error as exc:
                raise ConfigurationError(
                    f"{PATTERNS_ENV}: line {number}: not a valid pattern {line!r}: {exc}"
                ) from exc
    return Redactor(enabled=switch == "on", extra=tuple(extra))
