"""Setup diagnostics shared by the ``doctor`` CLI command and the ``mt4_doctor``
MCP tool.

Runs offline-safe local checks plus a per-host SSH probe (required tools,
systemd units, data dirs) and returns structured :class:`Check` rows that either
surface can render. The probe is verified for completeness, so a truncated or
malformed result fails closed rather than reporting a misleading pass.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Literal

from . import operations, scripts, ssh
from .auth import secrets_file
from .errors import RemoteCommandError
from .models import Host, Registry, Terminal

Status = Literal["ok", "warn", "fail"]

# Core tools the remote scripts depend on; the probe must report each as present.
_CORE_TOOLS = ("systemctl", "ss", "getent", "stat", "base64")


@dataclass(frozen=True, slots=True)
class Check:
    """One diagnostic line: ``ok`` pass, ``warn`` advisory, ``fail`` problem."""

    label: str
    status: Status
    detail: str = ""


def _secrets_check() -> Check:
    path = secrets_file()
    if not path.is_file():
        return Check("secrets file", "ok", f"none ({path}); pass passwords explicitly")
    if os.name == "posix" and (path.stat().st_mode & 0o077):
        return Check("secrets file", "fail", f"{path} is group/other-readable — chmod 600")
    return Check("secrets file", "ok", str(path))


async def _probe_host(host: Host, terms: list[Terminal]) -> list[Check]:
    specs = [(t.id, t.service, t.data_dir) for t in terms]
    try:
        result = await ssh.run(host, scripts.build_doctor_script(specs), timeout=30.0)
    except RemoteCommandError as exc:
        return [Check(f"host {host.id}", "fail", f"unreachable over SSH: {exc.stderr or exc}")]

    tool_status: dict[str, str] = {}
    xtools: dict[str, str] = {}
    term_lines: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(scripts.SEP)
        if parts[0] == "TOOL" and len(parts) == 3:
            tool_status[parts[1]] = parts[2]
        elif parts[0] == "XTOOL" and len(parts) == 3:
            xtools[parts[1]] = parts[2]
        elif parts[0] == "TERM" and len(parts) == 4:
            term_lines[parts[1]] = (parts[2], parts[3])

    # Each core tool must be explicitly reported as ok; "not reported" (e.g. a
    # truncated probe) counts against the host rather than passing silently.
    bad_core = [t for t in _CORE_TOOLS if tool_status.get(t) != "ok"]
    checks: list[Check] = [
        Check(
            f"host {host.id}",
            "fail" if bad_core else "ok",
            f"missing/unverified tools: {', '.join(bad_core)}"
            if bad_core
            else f"reachable, core tools present ({host.ssh})",
        )
    ]

    # Screenshots need a capture tool (imagemagick `import` *or* scrot) plus xdotool.
    shot_problems = []
    if xtools.get("import") != "ok" and xtools.get("scrot") != "ok":
        shot_problems.append("no capture tool (install imagemagick or scrot)")
    if xtools.get("xdotool") != "ok":
        shot_problems.append("xdotool missing")
    if shot_problems:
        checks.append(
            Check(
                f"host {host.id} screenshots",
                "warn",
                "; ".join(shot_problems) + " — mt4_screenshot unavailable",
            )
        )

    for term in terms:
        unit, dd = term_lines.get(term.id, ("", ""))
        if not unit and not dd:
            checks.append(
                Check(
                    f"terminal {term.id}", "fail", "doctor probe did not report this terminal"
                )
            )
            continue
        problems = []
        if unit != "found":
            problems.append(
                f"systemd unit {term.service!r} not found"
                if unit == "notfound"
                else f"unit status {unit!r}"
            )
        if dd != "ok":
            problems.append(
                f"data_dir {term.data_dir!r} missing"
                if dd == "missing"
                else f"data_dir status {dd!r}"
            )
        checks.append(
            Check(
                f"terminal {term.id}",
                "fail" if problems else "ok",
                "; ".join(problems) if problems else "unit + data_dir present",
            )
        )
    return checks


async def run_diagnostics(registry: Registry) -> list[Check]:
    """Run all diagnostics and return them in a stable, readable order."""
    checks: list[Check] = [
        Check(
            "registry",
            "ok",
            f"{len(registry.terminals)} terminals on {len(registry.hosts)} hosts",
        ),
        _secrets_check(),
    ]
    grouped = registry.by_host()
    # Probe every configured host, including any with no terminals.
    host_results = await asyncio.gather(
        *(_probe_host(registry.host(hid), grouped.get(hid, [])) for hid in registry.hosts)
    )
    for chunk in host_results:
        checks.extend(chunk)

    # Broker/login health — doctor must not report "all passed" while terminals
    # are active but not connected to a broker.
    statuses = await operations.status(registry)
    down = sorted(s.id for s in statuses if s.connected is False)
    if down:
        checks.append(
            Check(
                "broker connections",
                "warn",
                f"{len(down)} active but not connected: {', '.join(down)} "
                "— run mt4_login if not logged in",
            )
        )
    else:
        connected = sum(s.connected is True for s in statuses)
        checks.append(Check("broker connections", "ok", f"{connected} connected"))
    return checks


def format_checks(checks: list[Check]) -> str:
    """Render checks as an aligned ✓/!/✗ checklist."""
    glyph = {"ok": "✓", "warn": "!", "fail": "✗"}
    width = max((len(c.label) for c in checks), default=0)
    lines = [
        f"{glyph.get(c.status, '?')} {c.label:<{width}}  {c.detail}".rstrip() for c in checks
    ]
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    summary = (
        "all checks passed" if not fails and not warns else f"{fails} failed, {warns} warnings"
    )
    lines.append(f"\n{summary}")
    return "\n".join(lines)
