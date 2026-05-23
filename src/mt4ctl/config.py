"""Load and validate the terminal registry from YAML.

The registry path is resolved in this order (first hit wins):

1. the ``path`` argument to :func:`load_registry`
2. the ``MT4CTL_CONFIG`` environment variable
3. ``$XDG_CONFIG_HOME/mt4ctl/terminals.yaml`` (or ``~/.config/...``)
4. ``./terminals.yaml`` in the current working directory

This keeps real infrastructure details out of the source tree: ship the
``examples/terminals.example.yaml`` template, keep the populated file private.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import Env, Host, HostKind, Registry, Terminal


def candidate_paths() -> list[Path]:
    """Return the registry search path, highest precedence first."""
    paths: list[Path] = []
    if env := os.environ.get("MT4CTL_CONFIG"):
        paths.append(Path(env).expanduser())
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    paths.append(base / "mt4ctl" / "terminals.yaml")
    paths.append(Path.cwd() / "terminals.yaml")
    return paths


def resolve_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Find the registry file, raising :class:`ConfigError` if none exists."""
    if path is not None:
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise ConfigError(f"registry file not found: {resolved}")
        return resolved
    for candidate in candidate_paths():
        if candidate.is_file():
            return candidate
    searched = "\n  ".join(str(p) for p in candidate_paths())
    raise ConfigError(
        "no registry file found. Set MT4CTL_CONFIG or create one of:\n  "
        + searched
        + "\nSee examples/terminals.example.yaml for the format."
    )


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _build_host(host_id: str, raw: dict[str, Any]) -> Host:
    where = f"host {host_id!r}"
    try:
        kind = HostKind(raw.get("kind", "native"))
    except ValueError:
        raise ConfigError(
            f"{where}: invalid kind {raw.get('kind')!r} (use native or wsl)"
        ) from None
    try:
        return Host(
            id=host_id,
            ssh=str(_require(raw, "ssh", where)),
            kind=kind,
            wsl_distro=raw.get("wsl_distro"),
            broker_host=raw.get("broker_host"),
        )
    except ValueError as exc:  # invariant from Host.__post_init__
        raise ConfigError(str(exc)) from None


def _build_terminal(term_id: str, raw: dict[str, Any], hosts: dict[str, Host]) -> Terminal:
    where = f"terminal {term_id!r}"
    host_id = str(_require(raw, "host", where))
    if host_id not in hosts:
        raise ConfigError(f"{where}: references unknown host {host_id!r}")
    try:
        env = Env(raw.get("env", "demo"))
    except ValueError:
        raise ConfigError(
            f"{where}: invalid env {raw.get('env')!r} (use demo or live)"
        ) from None
    account = raw.get("account")
    return Terminal(
        id=term_id,
        host=host_id,
        service=str(_require(raw, "service", where)),
        data_dir=str(_require(raw, "data_dir", where)),
        display=str(raw.get("display", ":0")),
        account=str(account) if account is not None else None,
        env=env,
        window_match=raw.get("window_match"),
    )


def parse_registry(data: dict[str, Any]) -> Registry:
    """Build a :class:`Registry` from already-parsed YAML data."""
    if not isinstance(data, dict):
        raise ConfigError("registry root must be a mapping")
    raw_hosts = data.get("hosts") or {}
    raw_terminals = data.get("terminals") or {}
    if not raw_hosts:
        raise ConfigError("registry defines no hosts")
    if not raw_terminals:
        raise ConfigError("registry defines no terminals")

    hosts = {hid: _build_host(hid, raw) for hid, raw in raw_hosts.items()}
    terminals = {tid: _build_terminal(tid, raw, hosts) for tid, raw in raw_terminals.items()}
    return Registry(hosts=hosts, terminals=terminals)


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry:
    """Resolve, read, and validate the registry file."""
    resolved = resolve_path(path)
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved}: invalid YAML: {exc}") from None
    if data is None:
        raise ConfigError(f"{resolved}: file is empty")
    return parse_registry(data)
