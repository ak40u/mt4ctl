"""Credential resolution for terminal logins.

Resolution order (first hit wins), mirroring common CLI conventions:

1. an explicit ``password`` passed by the caller
2. ``MT4CTL_PASSWORD_<account>`` environment variable
3. the ``<account>`` key in a JSON secrets file
   (``$MT4CTL_CREDENTIALS`` or ``$XDG_CONFIG_HOME/mt4ctl/credentials.json``)

Passwords are never logged, echoed, or written anywhere except the transient
remote login config, which the login flow shreds after use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import CredentialError


def secrets_file() -> Path:
    """Return the path of the JSON secrets file (may not exist)."""
    if env := os.environ.get("MT4CTL_CREDENTIALS"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mt4ctl" / "credentials.json"


def resolve_password(account: str, explicit: str | None = None) -> str:
    """Return the password for *account*, raising :class:`CredentialError` if absent."""
    if explicit:
        return explicit

    if env := os.environ.get(f"MT4CTL_PASSWORD_{account}"):
        return env

    path = secrets_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CredentialError(f"could not read secrets file {path}: {exc}") from None
        if isinstance(data, dict) and account in data:
            return str(data[account])

    raise CredentialError(
        f"no password for account {account!r}. Provide it via the password "
        f"argument, the MT4CTL_PASSWORD_{account} env var, or the "
        f"{account!r} key in {path}."
    )
