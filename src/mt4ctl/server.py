"""The MCP server: thin tool wrappers over :mod:`mt4ctl.operations` and friends.

Run with ``mt4ctl`` (console script) or ``python -m mt4ctl``. Tools speak the
stdio transport by default — the form MCP clients (Claude Desktop, Claude Code)
spawn locally.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage

from . import diagnostics, operations
from . import login as login_mod
from .config import load_registry
from .errors import Mt4ctlError
from .models import DeployPlan, DeployResult, Registry, TerminalStatus

mcp = FastMCP(
    "mt4ctl",
    instructions=(
        "Manage headless MetaTrader terminals running under Wine + systemd on "
        "remote hosts. Use mt4_list to discover terminals, mt4_status to check "
        "health, mt4_logs to inspect activity. mt4_control and mt4_login mutate "
        "state; for terminals tagged env=live they require confirm=true."
    ),
)


@functools.lru_cache(maxsize=1)
def registry() -> Registry:
    """Load and cache the registry for the server's lifetime."""
    return load_registry()


R = TypeVar("R")


def _guard(fn: Callable[..., Awaitable[R]]) -> Callable[..., Awaitable[R | str]]:
    """Turn expected errors into clean tool messages instead of stack traces.

    Unknown-target and config problems raise :class:`Mt4ctlError`; invalid
    arguments raise :class:`ValueError`. Both become a concise ``error: …``
    string the agent can act on, for every tool uniformly.
    """

    @functools.wraps(fn)
    async def wrapper(*args: object, **kwargs: object) -> R | str:
        try:
            return await fn(*args, **kwargs)
        except (Mt4ctlError, ValueError) as exc:
            return f"error: {exc}"

    return wrapper


def _diagnose(s: TerminalStatus) -> tuple[str, str]:
    """Return ``(reason, next_action)`` for a non-healthy terminal, else ``("", "")``.

    Turns a bare ``active/down`` into something the operator can act on instead of
    guessing why a terminal is unhealthy.
    """
    if s.healthy:
        return ("", "")
    if s.service_state == "unknown":
        return ("host unreachable", "run `mt4_doctor`; check the SSH alias and key")
    if s.service_state != "active":
        return (f"unit {s.service_state}", "run `mt4_control <id> start`")
    if s.connected is None:
        return ("connection unknown", "run `mt4_doctor` (attribution needs same-user or root)")
    return (
        "no broker connection",
        "check `mt4_logs <id>`; run `mt4_login <id>` if not logged in",
    )


def _fmt_status(rows: list[TerminalStatus]) -> str:
    if not rows:
        return "no terminals match."
    header = f"{'TERMINAL':<12} {'ENV':<5} {'ACCOUNT':<12} {'SERVICE':<9} {'CONN':<6} {'LOG AGE':<8}"
    lines = [header, "-" * len(header)]
    # group non-healthy terminals by (reason, action) so the footer stays compact
    next_steps: dict[tuple[str, str], list[str]] = {}
    for s in rows:
        conn = {True: "up", False: "down", None: "?"}[s.connected]
        age = "-" if s.log_age_seconds is None else f"{s.log_age_seconds}s"
        reason, action = _diagnose(s)
        detail = f"  <- {reason}" if reason else ""
        lines.append(
            f"{s.id:<12} {s.env.value:<5} {(s.account or '-'):<12} "
            f"{s.service_state:<9} {conn:<6} {age:<8}{detail}"
        )
        if action:
            next_steps.setdefault((reason, action), []).append(s.id)
    if next_steps:
        lines.append("\nnext steps:")
        for (reason, action), ids in next_steps.items():
            lines.append(f"  {reason} ({', '.join(ids)}): {action}")
    return "\n".join(lines)


def _fmt_deploy_plan(plan: DeployPlan) -> str:
    """Render a (dry-run) plan: what would change, what stays foreign, why."""
    lines: list[str] = []
    for label, items in (("add", plan.add), ("update", plan.update), ("remove", plan.remove)):
        if items:
            lines.append(f"{label} ({len(items)}):")
            lines.extend(f"  {p}" for p in items)
    if plan.unchanged:
        lines.append(f"unchanged: {len(plan.unchanged)}")
    if plan.foreign:
        lines.append(f"foreign — left untouched ({len(plan.foreign)}):")
        lines.extend(f"  {p}" for p in plan.foreign)
    if plan.conflicts:
        lines.append(f"REFUSED — would overwrite files mt4ctl does not manage ({len(plan.conflicts)}):")
        lines.extend(f"  {p}" for p in plan.conflicts)
        lines.append("  next: remove/rename them on the host, or drop them from the bundle")
    if plan.notes:
        lines.append("notes:")
        lines.extend(f"  - {n}" for n in plan.notes)
    header = (
        f"plan: +{len(plan.add)} ~{len(plan.update)} -{len(plan.remove)}"
        if plan.has_changes or plan.conflicts
        else "no changes — terminal already matches the bundle"
    )
    return header + ("\n" + "\n".join(lines) if lines else "")


def _fmt_deploy_result(result: DeployResult) -> str:
    """Render an applied deploy: counts, backup, restart, and the verify report."""
    p = result.plan
    lines = [
        f"{result.terminal}: deployed +{len(p.add)} ~{len(p.update)} -{len(p.remove)} "
        f"(unchanged {len(p.unchanged)})"
    ]
    if p.foreign:
        lines.append(f"foreign left untouched: {len(p.foreign)}")
    if result.backup_path:
        lines.append(f"backup: {result.backup_path}")
    lines.append(
        "restarted: yes" if result.restarted else "restarted: NO — run `mt4_control <id> start`"
    )
    lines.append(f"verify: {'ok' if result.verify_ok else 'NOT confirmed'} ({result.verify_detail})")
    if not result.verify_ok:
        lines.append(
            "  note: verify is report-only and did NOT revert the deploy; "
            "check `mt4_logs`/`mt4_status`. Recovery = re-deploy the previous bundle."
        )
    if p.notes:
        lines.append("notes:")
        lines.extend(f"  - {n}" for n in p.notes)
    return "\n".join(lines)


@mcp.tool()
@_guard
async def mt4_list() -> str:
    """List configured terminals with host, account, and environment.

    Read-only and offline (no SSH). Use this first to learn which terminal ids
    exist before calling status/logs/control.
    """
    reg = registry()
    rows = [
        f"{t.id:<12} host={t.host:<14} account={t.account or '-':<12} env={t.env.value}"
        for t in reg.terminals.values()
    ]
    hosts = ", ".join(reg.hosts) or "(none)"
    return f"hosts: {hosts}\n\n" + ("\n".join(rows) or "no terminals configured.")


@mcp.tool()
@_guard
async def mt4_status(terminal: str = "all") -> str:
    """Report health of one terminal or all of them.

    Queries hosts concurrently and shows, per terminal: systemd service state,
    broker connection (per-terminal, via socket attribution), and how long since
    the log was last written. ``CONN=up`` plus ``SERVICE=active`` means healthy.

    Args:
        terminal: a terminal id, or "all" (default).
    """
    ids = None if terminal == "all" else [terminal]
    rows = await operations.status(registry(), ids)
    return _fmt_status(rows)


@mcp.tool()
@_guard
async def mt4_logs(terminal: str, pattern: str = "", lines: int = 50) -> str:
    """Return the tail of a terminal's newest log file.

    Args:
        terminal: terminal id.
        pattern: optional case-insensitive regex to grep (e.g. "login|error").
        lines: number of trailing lines to return (1-1000).
    """
    return await operations.logs(registry(), terminal, pattern=pattern or None, lines=lines)


@mcp.tool()
@_guard
async def mt4_control(terminal: str, action: str, confirm: bool = False) -> str:
    """Start, stop, or restart a terminal's systemd unit.

    Mutating a live terminal requires confirm=true.

    Args:
        terminal: terminal id.
        action: one of "start", "stop", "restart".
        confirm: must be true to act on a terminal tagged env=live.
    """
    st = await operations.control(registry(), terminal, action, confirm=confirm)
    conn = {True: "up", False: "down", None: "?"}[st.connected]
    return f"{terminal}: {action} done -> service={st.service_state}, conn={conn}"


@mcp.tool()
@_guard
async def mt4_login(
    terminal: str,
    server: str,
    account: str = "",
    password: str = "",
    confirm: bool = False,
) -> str:
    """Perform a one-time headless login so a migrated terminal can auto-reconnect.

    Needed when a terminal was copied to a new host: MetaTrader's saved password
    is machine-bound and must be re-entered once. After this succeeds the unit
    auto-logins on every restart. Live terminals require confirm=true.

    Args:
        terminal: terminal id.
        server: broker server name, e.g. "ExampleBroker-Demo".
        account: login number; defaults to the terminal's configured account.
        password: explicit password; otherwise resolved from env/secrets file.
        confirm: must be true to act on a terminal tagged env=live.
    """
    return await login_mod.login(
        registry(),
        terminal,
        account=account or None,
        server=server,
        password=password or None,
        confirm=confirm,
    )


@mcp.tool()
@_guard
async def mt4_screenshot(terminal: str) -> McpImage:
    """Capture a screenshot of a terminal's window (PNG).

    Useful to visually confirm the chart, the AutoTrading state, and the EA
    smiley. On shared-display hosts the target window is raised first.

    Args:
        terminal: terminal id.
    """
    path = await operations.screenshot(registry(), terminal)
    return McpImage(path=str(path))


@mcp.tool()
@_guard
async def mt4_doctor() -> str:
    """Diagnose the mt4ctl setup without mutating anything.

    Checks the registry, the secrets-file permissions, and — per host — SSH
    reachability, required remote tools, systemd units, and data directories.
    Run this when a terminal is unexpectedly ``unknown`` or `mt4_status` looks
    wrong. Read-only and safe.
    """
    checks = await diagnostics.run_diagnostics(registry())
    return diagnostics.format_checks(checks)


@mcp.tool()
@_guard
async def mt4_ea_list(terminal: str = "all") -> str:
    """List the expert advisors (strategies) attached to terminals.

    For a single terminal, lists every attached EA; for "all", shows the count
    per terminal. Read-only (parses the terminal's chart files).

    Args:
        terminal: a terminal id, or "all" (default).
    """
    reg = registry()
    ids = list(reg.terminals) if terminal == "all" else [terminal]
    reports = await asyncio.gather(*(operations.experts(reg, t) for t in ids))
    if len(ids) == 1:
        r = reports[0]
        if r.error:
            return f"{ids[0]}: unreachable — {r.error}"
        names = "\n".join(f"  {e.short_name}" for e in r.experts) or "  (none)"
        return f"{ids[0]}: {len(r.experts)} experts\n{names}"
    return "\n".join(
        f"{r.terminal:<12} unreachable — {r.error}"
        if r.error
        else f"{r.terminal:<12} {len(r.experts):>4} experts"
        for r in reports
    )


@mcp.tool()
@_guard
async def mt4_autotrading(terminal: str = "all") -> str:
    """Report whether algo-trading is enabled — terminal master switch + per-EA.

    Shows the terminal-level AutoTrading button (from terminal.ini) and how many
    attached experts have live-trading enabled. Flags terminals whose master is
    off (nothing trades) or whose experts have non-uniform/disabled flags.

    Note: the per-EA live-trading flag is a best-effort decode of the MT4
    chart-expert bitmask; the terminal master switch is authoritative.

    Args:
        terminal: a terminal id, or "all" (default).
    """
    reg = registry()
    ids = list(reg.terminals) if terminal == "all" else [terminal]
    reports = await asyncio.gather(*(operations.experts(reg, t) for t in ids))
    lines = [f"{'TERMINAL':<12} {'AUTOTRADING':<12} EXPERTS"]
    for r in reports:
        if r.error:
            lines.append(f"{r.terminal:<12} {'?':<12} unreachable — {r.error}")
            continue
        master = {True: "on", False: "OFF", None: "?"}[r.master]
        total = len(r.experts)
        live = sum(e.live_trading is True for e in r.experts)
        off = [e.short_name for e in r.experts if e.live_trading is False]
        unknown = sum(e.live_trading is None for e in r.experts)
        note = ""
        if r.master is False:
            note = "  <- master AutoTrading OFF; nothing trades"
        elif off:
            note = f"  <- {len(off)} EA live-trading off: {', '.join(off[:5])}"
        elif unknown:
            note = f"  <- {unknown} EA flags unreadable"
        lines.append(f"{r.terminal:<12} {master:<12} {live}/{total} live{note}")
    return "\n".join(lines)


@mcp.tool()
@_guard
async def mt4_info(terminal: str = "all") -> str:
    """Report each terminal's build, broker server, and last broker ping.

    Read-only (parsed from the terminal's log). Useful to confirm what build and
    broker a terminal is on and its connection latency.

    Args:
        terminal: a terminal id, or "all" (default).
    """
    reg = registry()
    ids = list(reg.terminals) if terminal == "all" else [terminal]
    infos = await asyncio.gather(*(operations.info(reg, t) for t in ids))
    lines = [f"{'TERMINAL':<12} {'BUILD':<22} {'SERVER':<18} PING"]
    for i in infos:
        if i.error:
            lines.append(f"{i.terminal:<12} unreachable — {i.error}")
            continue
        ping = "-" if i.ping_ms is None else f"{i.ping_ms:.0f}ms"
        lines.append(f"{i.terminal:<12} {(i.build or '-'):<22} {(i.server or '-'):<18} {ping}")
    return "\n".join(lines)


@mcp.tool()
@_guard
async def mt4_deploy(
    terminal: str, bundle: str, dry_run: bool = False, confirm: bool = False
) -> str:
    """Deploy a strategy bundle to a terminal — idempotent, managed-subset.

    Pushes pre-built charts + experts so the terminal's *managed* files match the
    bundle, while leaving foreign files (e.g. a watchdog's chart) untouched. This
    is apply-only: it does NOT select strategies, set lots/magic, generate charts,
    or compile — the caller owns the bundle's contents.

    `bundle` is a LOCAL directory on this machine (read here and pushed over SSH —
    NOT a path on the remote host). It mirrors the MT4 layout:

        <bundle>/
          profiles/default/<name>.chr        # ready charts (one expert each)
          MQL4/Experts/<folder>/<ea>.ex4     # the experts those charts reference

    Always run with dry_run=true first to preview the add/update/remove/foreign
    plan. Re-running the same bundle is a no-op (reports "no changes"). A terminal
    tagged env=live requires confirm=true. There is no rollback command: to
    recover, re-deploy the previous bundle (a pre-apply backup is also retained
    and is restored automatically if an apply fails).

    Args:
        terminal: terminal id.
        bundle: local bundle directory path.
        dry_run: preview the plan without changing anything (no lock, no upload).
        confirm: must be true to deploy to a terminal tagged env=live.
    """
    result = await operations.deploy(
        registry(), terminal, bundle, dry_run=dry_run, confirm=confirm
    )
    return _fmt_deploy_plan(result.plan) if result.dry_run else _fmt_deploy_result(result)


def serve() -> None:
    """Run the MCP server over stdio (the form MCP clients launch)."""
    mcp.run()


if __name__ == "__main__":
    serve()
