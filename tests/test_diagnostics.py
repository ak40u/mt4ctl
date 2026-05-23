"""Setup diagnostics: parsing host probes and rendering checks."""

from __future__ import annotations

import os

import pytest

from mt4ctl import diagnostics, ssh
from mt4ctl.diagnostics import Check, format_checks, run_diagnostics
from mt4ctl.errors import RemoteCommandError
from mt4ctl.ssh import CommandResult

_ALL_OK = (
    "TOOL|systemctl|ok\nTOOL|ss|ok\nTOOL|getent|ok\nTOOL|stat|ok\nTOOL|base64|ok\n"
    "XTOOL|import|ok\nXTOOL|scrot|ok\nXTOOL|xdotool|ok\n"
    "TERM|demo1|found|ok\nTERM|live-main|found|ok\n"
)


def test_format_checks_renders_glyphs_and_summary():
    out = format_checks([Check("a", "ok", "fine"), Check("b", "fail", "broke")])
    assert "✓ a" in out and "✗ b" in out
    assert "1 failed" in out


async def test_run_diagnostics_all_ok(registry, monkeypatch):
    async def fake_run(host, script, **kw):
        return CommandResult(0, _ALL_OK, "")

    monkeypatch.setattr(ssh, "run", fake_run)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", "/nonexistent/creds.json")
    by_label = {c.label: c for c in await run_diagnostics(registry)}
    assert by_label["host demo-box"].status == "ok"
    assert by_label["host live-vps"].status == "ok"
    assert by_label["terminal demo1"].status == "ok"
    assert by_label["terminal live-main"].status == "ok"


async def test_run_diagnostics_missing_tool_and_unit(registry, monkeypatch):
    out = "TOOL|systemctl|ok\nTOOL|ss|missing\nTERM|demo1|notfound|missing\n"

    async def fake_run(host, script, **kw):
        return CommandResult(0, out, "")

    monkeypatch.setattr(ssh, "run", fake_run)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", "/nonexistent/creds.json")
    by_label = {c.label: c for c in await run_diagnostics(registry)}
    assert by_label["host demo-box"].status == "fail"
    assert "ss" in by_label["host demo-box"].detail
    assert by_label["terminal demo1"].status == "fail"


async def test_run_diagnostics_truncated_probe_fails_closed(registry, monkeypatch):
    async def fake_run(host, script, **kw):
        return CommandResult(0, "TOOL|systemctl|ok\n", "")  # truncated/incomplete

    monkeypatch.setattr(ssh, "run", fake_run)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", "/nonexistent/creds.json")
    by_label = {c.label: c for c in await run_diagnostics(registry)}
    assert by_label["host demo-box"].status == "fail"  # core tools unverified
    assert by_label["terminal demo1"].status == "fail"  # terminal not reported


async def test_run_diagnostics_probes_host_without_terminals(monkeypatch):
    from mt4ctl.config import parse_registry

    reg = parse_registry(
        {
            "hosts": {"h1": {"ssh": "h1"}, "h2": {"ssh": "h2"}},
            "terminals": {
                "t": {"host": "h1", "service": "s", "data_dir": "/d", "account": "1"}
            },
        }
    )
    probed: list[str] = []

    async def fake_run(host, script, **kw):
        probed.append(host.id)
        return CommandResult(0, _ALL_OK, "")

    monkeypatch.setattr(ssh, "run", fake_run)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", "/nonexistent/creds.json")
    labels = {c.label for c in await run_diagnostics(reg)}
    assert "host h2" in labels and "h2" in probed  # host with no terminals still probed


async def test_run_diagnostics_flags_disconnected_terminals(registry, monkeypatch):
    async def fake_run(host, script, **kw):
        if "checkterm" in script:  # the doctor probe
            return CommandResult(0, _ALL_OK, "")
        # the status probe: demo1 active but down, live-main active + connected
        out = "TERM|demo1|active|5|0|\nTERM|live-main|active|3|2|login on B\n"
        return CommandResult(0, out, "")

    monkeypatch.setattr(ssh, "run", fake_run)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", "/nonexistent/creds.json")
    by_label = {c.label: c for c in await run_diagnostics(registry)}
    assert by_label["broker connections"].status == "warn"
    assert "demo1" in by_label["broker connections"].detail
    assert "mt4_login" in by_label["broker connections"].detail


async def test_run_diagnostics_unreachable_host(registry, monkeypatch):
    async def boom(host, script, **kw):
        raise RemoteCommandError(host.id, 255, "connection refused")

    monkeypatch.setattr(ssh, "run", boom)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", "/nonexistent/creds.json")
    by_label = {c.label: c for c in await run_diagnostics(registry)}
    assert by_label["host demo-box"].status == "fail"
    assert "unreachable" in by_label["host demo-box"].detail


def test_secrets_check_flags_loose_permissions(monkeypatch, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permission check")
    f = tmp_path / "creds.json"
    f.write_text("{}")
    f.chmod(0o644)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", str(f))
    assert diagnostics._secrets_check().status == "fail"
