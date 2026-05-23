"""Setup diagnostics shared by the ``doctor`` CLI command and the ``mt4_doctor``
MCP tool.

Runs offline-safe local checks plus a per-host SSH probe (required tools,
systemd units, data dirs) and returns structured :class:`Check` rows that either
surface can render.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from . import scripts, ssh
from .auth import secrets_file
from .errors import RemoteCommandError
from .models import Host, Registry, Terminal


@dataclass(frozen=True, slots=True)
class Check:
    """One diagnostic line: ``ok`` pass, ``warn`` advisory, ``fail`` problem."""

    label: str
    status: str  # "ok" | "warn" | "fail"
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

    missing_core: list[str] = []
    xtools: dict[str, str] = {}
    term_lines: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(scripts.SEP)
        if parts[0] == "TOOL" and len(parts) == 3 and parts[2] == "missing":
            missing_core.append(parts[1])
        elif parts[0] == "XTOOL" and len(parts) == 3:
            xtools[parts[1]] = parts[2]
        elif parts[0] == "TERM" and len(parts) == 4:
            term_lines[parts[1]] = (parts[2], parts[3])

    checks = [
        Check(
            f"host {host.id}",
            "fail" if missing_core else "ok",
            f"missing required tools: {', '.join(missing_core)}"
            if missing_core
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
        unit, dd = term_lines.get(term.id, ("?", "?"))
        problems = []
        if unit == "notfound":
            problems.append(f"systemd unit {term.service!r} not found")
        if dd == "missing":
            problems.append(f"data_dir {term.data_dir!r} missing")
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
    checks = [
        Check(
            "registry",
            "ok",
            f"{len(registry.terminals)} terminals on {len(registry.hosts)} hosts",
        ),
        _secrets_check(),
    ]
    grouped = registry.by_host()
    host_results = await asyncio.gather(
        *(_probe_host(registry.host(hid), terms) for hid, terms in grouped.items())
    )
    for chunk in host_results:
        checks.extend(chunk)
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
