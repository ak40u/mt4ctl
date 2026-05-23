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


async def test_login_live_requires_confirm(registry, monkeypatch):
    async def fail(*a, **k):
        raise AssertionError("ssh.run must not be called without confirmation")

    monkeypatch.setattr(ssh, "run", fail)
    with pytest.raises(ConfirmationRequiredError):
        await login_mod.login(registry, "live-main", server="Broker", confirm=False)


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
