"""Typed errors with actionable messages.

Errors raised here are caught at the MCP boundary and surfaced to the calling
agent verbatim, so each message should tell the agent *how to recover* rather
than just *what went wrong*.
"""

from __future__ import annotations


class Mt4ctlError(Exception):
    """Base class for all expected, user-facing errors."""


class ConfigError(Mt4ctlError):
    """The registry file is missing, malformed, or internally inconsistent."""


class UnknownTargetError(Mt4ctlError):
    """A referenced terminal or host does not exist in the registry."""


class RemoteCommandError(Mt4ctlError):
    """A command executed over SSH returned a non-zero exit status."""

    def __init__(self, host: str, returncode: int, stderr: str) -> None:
        self.host = host
        self.returncode = returncode
        self.stderr = stderr.strip()
        detail = self.stderr or "(no stderr)"
        super().__init__(f"remote command on {host!r} failed (exit {returncode}): {detail}")


class ConfirmationRequiredError(Mt4ctlError):
    """A mutating operation targeted a live terminal without ``confirm=true``."""

    def __init__(self, terminal_id: str, action: str) -> None:
        self.terminal_id = terminal_id
        self.action = action
        super().__init__(
            f"refusing to {action} live terminal {terminal_id!r} without "
            f"confirmation; pass confirm=true to proceed"
        )


class CredentialError(Mt4ctlError):
    """A credential could not be resolved for a login operation."""
