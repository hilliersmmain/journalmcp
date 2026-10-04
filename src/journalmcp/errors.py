"""The typed errors the core raises. A front catches `JournalmcpError` alone."""


class JournalmcpError(Exception):
    """Base class: every error the core raises on purpose."""


class InvalidArgument(JournalmcpError):
    """A caller value failed validation before any argv was built."""


class CommandTimeout(JournalmcpError):
    """A subprocess ran past its timeout and was killed."""


class CommandFailed(JournalmcpError):
    """A subprocess could not start, or exited non-zero."""


class MalformedOutput(JournalmcpError):
    """A subprocess printed output the parser does not recognise."""


class FixtureMissing(JournalmcpError):
    """The fixture runner was asked for an argv it has no recording of."""


class ConfigurationError(JournalmcpError):
    """An environment setting the core reads is not one it accepts."""
