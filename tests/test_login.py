"""Login bootstrap: script safety and the live-confirmation guard."""

from __future__ import annotations

import pytest

from mt4ctl import login as login_mod
from mt4ctl import ssh
from mt4ctl.config import parse_registry
from mt4ctl.errors import ConfirmationRequiredError, Mt4ctlError
from mt4ctl.login import build_login_script


def test_script_uses_quoted_heredoc_to_disable_expansion():
    # A quoted delimiter (<<'INICFG') prevents the shell from expanding $(...),
    # backticks, or $VAR inside the credentials block.
    out = build_login_script("svc", "/d", "1000001", "Broker-Demo", "pw")
    assert "<<'INICFG'" in out
    assert "<<INICFG" not in out.replace("<<'INICFG'", "")


def test_script_writes_credentials_literally():
    out = build_login_script("svc", "/d", "1000001", "Broker-Demo", "p@ss")
    assert "Login=1000001" in out
    assert "Server=Broker-Demo" in out
    assert "Password=p@ss" in out


def test_dangerous_password_is_written_not_executed():
    # A password full of shell metacharacters must appear verbatim in the ini
    # body, never as a command substitution.
    nasty = "a$(touch /tmp/pwned)`whoami`b"
    out = build_login_script("svc", "/d", "1000001", "Broker-Demo", nasty)
    assert f"Password={nasty}" in out


@pytest.mark.parametrize("field_value", ["12\n34", "ab\rcd", "x\nINICFG"])
def test_newline_in_credentials_rejected(field_value):
    with pytest.raises(Mt4ctlError, match="must not contain newlines"):
        build_login_script("svc", "/d", field_value, "Broker-Demo", "pw")
    with pytest.raises(Mt4ctlError, match="must not contain newlines"):
        build_login_script("svc", "/d", "1000001", "Broker-Demo", field_value)


def test_script_kills_only_its_process_group():
    out = build_login_script("svc", "/d", "1000001", "Broker-Demo", "pw")
    assert "setsid wine terminal.exe" in out
    assert 'kill -TERM -"$PGID"' in out
    assert "wineserver -k" not in out  # would nuke sibling terminals


def test_script_uses_mktemp_chmod_and_cleanup_trap():
    out = build_login_script("svc", "/d", "1000001", "Broker-Demo", "pw")
    assert "mktemp " in out
    assert "chmod 600" in out
    assert "trap cleanup EXIT HUP INT TERM" in out


def test_script_uses_marker_newer_than_for_success():
    out = build_login_script("svc", "/d", "1000001", "Broker-Demo", "pw")
    assert '"$ACCFILE" -nt "$MARKER"' in out


async def test_login_live_requires_confirm(registry, monkeypatch):
    async def fail(*a, **k):
        raise AssertionError("ssh.run must not be called without confirmation")

    monkeypatch.setattr(ssh, "run", fail)
    with pytest.raises(ConfirmationRequiredError):
        await login_mod.login(registry, "live-main", server="Broker", confirm=False)


def _fake_ssh(login_line: str, *, start_fails: bool):
    from mt4ctl.errors import RemoteCommandError
    from mt4ctl.ssh import CommandResult

    async def fake_run(host, script, **kw):
        if "systemctl start" in script:
            if start_fails:
                raise RemoteCommandError("h", 1, "start failed")
            return CommandResult(0, "STATE|active|rc=0", "")
        if "systemctl stop" in script:
            return CommandResult(0, "STATE|inactive|rc=0", "")
        return CommandResult(0, login_line, "")  # the bootstrap script

    return fake_run


async def test_login_reports_restart_failure_even_when_login_failed(registry, monkeypatch):
    monkeypatch.setattr(ssh, "run", _fake_ssh("LOGIN|ok=0", start_fails=True))
    msg = await login_mod.login(registry, "demo1", server="B", password="x")
    assert "did NOT confirm" in msg
    assert "restarting the unit failed" in msg  # not hidden


async def test_login_success_with_restart(registry, monkeypatch):
    monkeypatch.setattr(ssh, "run", _fake_ssh("LOGIN|ok=1", start_fails=False))
    msg = await login_mod.login(registry, "demo1", server="B", password="x")
    assert "logged in" in msg
    assert "restarted for auto-reconnect" in msg


async def test_login_success_but_restart_failed(registry, monkeypatch):
    monkeypatch.setattr(ssh, "run", _fake_ssh("LOGIN|ok=1", start_fails=True))
    msg = await login_mod.login(registry, "demo1", server="B", password="x")
    assert "logged in" in msg
    assert "restarting the unit failed" in msg


async def test_login_validates_before_stopping(registry, monkeypatch):
    calls: list[str] = []

    async def record(host, script, **kw):
        calls.append(script)
        from mt4ctl.ssh import CommandResult

        return CommandResult(0, "", "")

    monkeypatch.setattr(ssh, "run", record)
    with pytest.raises(Mt4ctlError, match="must not contain newlines"):
        await login_mod.login(registry, "demo1", server="Bad\nName", password="x")
    assert calls == []  # rejected before the unit was stopped


async def test_login_restarts_after_bootstrap_timeout(registry, monkeypatch):
    from mt4ctl.errors import RemoteCommandError
    from mt4ctl.ssh import CommandResult

    seen: list[str] = []

    async def fake(host, script, **kw):
        if "systemctl start" in script:
            seen.append("start")
            return CommandResult(0, "STATE|active|rc=0", "")
        if "systemctl stop" in script:
            seen.append("stop")
            return CommandResult(0, "STATE|inactive|rc=0", "")
        raise RemoteCommandError("h", 124, "command timed out")  # the bootstrap

    monkeypatch.setattr(ssh, "run", fake)
    msg = await login_mod.login(registry, "demo1", server="B", password="x")
    assert "start" in seen  # unit restarted despite the bootstrap failure
    assert "bootstrap failed" in msg


async def test_login_requires_an_account():
    # A terminal with no account in the registry, and none passed -> rejected
    # before any SSH call is made.
    reg = parse_registry(
        {
            "hosts": {"h": {"ssh": "h"}},
            "terminals": {"t": {"host": "h", "service": "s", "data_dir": "/d"}},
        }
    )
    with pytest.raises(Mt4ctlError, match="no account configured"):
        await login_mod.login(reg, "t", account=None, server="Broker")
