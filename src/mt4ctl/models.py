"""Domain models for the terminal registry.

The registry is intentionally plain data: a set of *hosts* (machines reachable
over SSH) and a set of *terminals* (MetaTrader instances managed by ``systemd``
on those hosts). Everything else in :mod:`mt4ctl` operates on these models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .errors import UnknownTargetError


class HostKind(StrEnum):
    """How commands are dispatched on a host.

    ``NATIVE``
        A plain Linux box. Commands run directly; root is obtained via ``sudo``.
    ``WSL``
        A Windows host running WSL2. Commands are wrapped in
        ``wsl -d <distro> -- ...`` and root is obtained via ``wsl -u root``.
    """

    NATIVE = "native"
    WSL = "wsl"


class Env(StrEnum):
    """Risk tier of a terminal.

    ``LIVE`` terminals trade real money, so mutating operations on them require
    an explicit ``confirm=true`` from the caller.
    """

    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class Host:
    """A machine reachable over SSH that runs one or more terminals."""

    id: str
    ssh: str
    """SSH destination — an alias from ``~/.ssh/config`` or ``user@host``."""
    kind: HostKind = HostKind.NATIVE
    wsl_distro: str | None = None
    """Required when :attr:`kind` is :data:`HostKind.WSL`."""
    broker_host: str | None = None
    """Optional broker hostname used to detect live connections via ``ss``."""

    def __post_init__(self) -> None:
        if self.kind is HostKind.WSL and not self.wsl_distro:
            raise ValueError(f"host {self.id!r} is kind=wsl but has no wsl_distro")


@dataclass(frozen=True, slots=True)
class Terminal:
    """A single MetaTrader terminal managed by ``systemd``."""

    id: str
    host: str
    """Id of the owning :class:`Host`."""
    service: str
    """The ``systemd`` unit name, e.g. ``mt4-demo3``."""
    data_dir: str
    """Absolute path to the terminal data folder (parent of ``logs/``)."""
    display: str = ":0"
    """X11 display used for screenshots, e.g. ``:99``."""
    account: str | None = None
    """Login number — labels output and locates the window for screenshots."""
    env: Env = Env.DEMO
    window_match: str | None = None
    """Override window-search string; defaults to :attr:`account`."""

    @property
    def window_query(self) -> str | None:
        """The string used to find this terminal's window with ``xdotool``."""
        return self.window_match or self.account


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    """Resolved runtime status of a terminal."""

    id: str
    host: str
    env: Env
    account: str | None
    service_state: str
    """Raw ``systemctl is-active`` value: ``active`` / ``inactive`` / ``failed``…"""
    connected: bool | None
    """True/False if a broker connection could be determined, else ``None``."""
    log_age_seconds: int | None
    """Seconds since the newest log file was written, or ``None`` if no logs."""
    last_event: str | None = None
    """Most recent connection-related log line, if any."""

    @property
    def healthy(self) -> bool:
        """A terminal is healthy when its service is active and it is connected."""
        return self.service_state == "active" and self.connected is True


@dataclass(frozen=True, slots=True)
class Registry:
    """The full set of configured hosts and terminals."""

    hosts: dict[str, Host] = field(default_factory=dict)
    terminals: dict[str, Terminal] = field(default_factory=dict)

    def terminal(self, terminal_id: str) -> Terminal:
        try:
            return self.terminals[terminal_id]
        except KeyError:
            raise UnknownTargetError(
                f"unknown terminal {terminal_id!r}; known: "
                f"{', '.join(sorted(self.terminals)) or '(none)'}"
            ) from None

    def host(self, host_id: str) -> Host:
        try:
            return self.hosts[host_id]
        except KeyError:
            raise UnknownTargetError(
                f"unknown host {host_id!r}; known: {', '.join(sorted(self.hosts)) or '(none)'}"
            ) from None

    def host_of(self, terminal: Terminal) -> Host:
        return self.host(terminal.host)

    def by_host(self) -> dict[str, list[Terminal]]:
        """Group terminals by their host id (stable order)."""
        grouped: dict[str, list[Terminal]] = {}
        for term in self.terminals.values():
            grouped.setdefault(term.host, []).append(term)
        return grouped
