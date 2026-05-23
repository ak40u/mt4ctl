"""High-level terminal operations: status, logs, screenshots, lifecycle control.

These functions take a :class:`~mt4ctl.models.Registry`, talk to hosts through
:mod:`mt4ctl.ssh`, and return domain objects or plain strings. The MCP server in
:mod:`mt4ctl.server` is a thin shell over this module.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from . import scripts, ssh
from .errors import ConfirmationRequiredError, RemoteCommandError
from .models import Env, Host, Registry, Terminal, TerminalStatus

CONTROL_ACTIONS = ("start", "stop", "restart")


def _parse_status_line(line: str, term: Terminal) -> TerminalStatus | None:
    parts = line.split(scripts.SEP)
    if len(parts) < 6 or parts[0] != "TERM":
        return None
    _, _id, state, age_s, estab_s, last = parts[:6]
    try:
        age = int(age_s)
    except ValueError:
        age = -1
    try:
        estab = int(estab_s)
    except ValueError:
        estab = -1
    return TerminalStatus(
        id=term.id,
        host=term.host,
        env=term.env,
        account=term.account,
        service_state=state or "unknown",
        connected=(estab > 0) if estab >= 0 else None,
        log_age_seconds=age if age >= 0 else None,
        last_event=last.strip() or None,
    )


async def _status_for_host(host: Host, terms: list[Terminal]) -> dict[str, TerminalStatus]:
    specs = [(t.id, t.service, t.data_dir) for t in terms]
    script = scripts.build_status_script(host.broker_host, specs)
    by_id = {t.id: t for t in terms}
    out: dict[str, TerminalStatus] = {}
    try:
        result = await ssh.run(host, script, timeout=30.0, check=False)
        for line in result.stdout.splitlines():
            if line.startswith("TERM" + scripts.SEP):
                tid = line.split(scripts.SEP)[1]
                if (term := by_id.get(tid)) and (st := _parse_status_line(line, term)):
                    out[tid] = st
    except RemoteCommandError:
        pass  # unreachable host -> emit 'unknown' below
    for term in terms:
        out.setdefault(
            term.id,
            TerminalStatus(
                id=term.id,
                host=term.host,
                env=term.env,
                account=term.account,
                service_state="unknown",
                connected=None,
                log_age_seconds=None,
                last_event="host unreachable",
            ),
        )
    return out


async def status(
    registry: Registry, terminal_ids: list[str] | None = None
) -> list[TerminalStatus]:
    """Resolve status for the given terminals (all of them when *terminal_ids* is None).

    Hosts are queried concurrently; results preserve registry order.
    """
    selected = (
        [registry.terminal(t) for t in terminal_ids]
        if terminal_ids
        else list(registry.terminals.values())
    )
    grouped: dict[str, list[Terminal]] = {}
    for term in selected:
        grouped.setdefault(term.host, []).append(term)

    results = await asyncio.gather(
        *(_status_for_host(registry.host(hid), terms) for hid, terms in grouped.items())
    )
    merged: dict[str, TerminalStatus] = {}
    for chunk in results:
        merged.update(chunk)
    return [merged[t.id] for t in selected]


async def logs(
    registry: Registry,
    terminal_id: str,
    *,
    pattern: str | None = None,
    lines: int = 50,
) -> str:
    """Return the tail of a terminal's newest log file, optionally grep-filtered."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    script = scripts.build_logs_script(term.data_dir, pattern, max(1, min(lines, 1000)))
    result = await ssh.run(host, script, timeout=30.0, check=False)
    return result.stdout.strip() or "(no output)"


async def control(
    registry: Registry,
    terminal_id: str,
    action: str,
    *,
    confirm: bool = False,
) -> TerminalStatus:
    """Run ``start``/``stop``/``restart`` on a terminal's systemd unit.

    Mutating a :data:`~mt4ctl.models.Env.LIVE` terminal requires ``confirm=True``.
    """
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"action must be one of {CONTROL_ACTIONS}, got {action!r}")
    term = registry.terminal(terminal_id)
    if term.env is Env.LIVE and not confirm:
        raise ConfirmationRequiredError(terminal_id, action)
    host = registry.host_of(term)
    script = scripts.build_control_script(term.service, action)
    # The script exits with systemctl's code; check=True surfaces a failed
    # start/stop/restart instead of reporting phantom success.
    await ssh.run(host, script, root=True, timeout=45.0, check=True)
    return (await status(registry, [terminal_id]))[0]


async def screenshot(
    registry: Registry, terminal_id: str, *, out_dir: Path | None = None
) -> Path:
    """Capture a terminal window and save the PNG locally, returning its path."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    remote_tmp = f"/tmp/mt4ctl-{uuid.uuid4().hex}.png"
    script = scripts.build_screenshot_script(term.display, term.window_query, remote_tmp)
    result = await ssh.run(host, script, timeout=45.0, check=False)
    if "OK " not in result.stdout:
        raise RemoteCommandError(host.id, 1, "screenshot capture produced no image")
    data = await ssh.fetch_bytes(host, remote_tmp)
    await ssh.run(host, f"rm -f {scripts.sh_quote(remote_tmp)}", check=False)

    out_dir = out_dir or Path.home() / ".cache" / "mt4ctl"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", term.id)
    local = out_dir / f"{safe_id}.png"
    local.write_bytes(data)
    return local
