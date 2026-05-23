"""Status parsing and the live-confirmation guard."""

from __future__ import annotations

import pytest

from mt4ctl import operations, ssh
from mt4ctl.errors import ConfirmationRequiredError
from mt4ctl.operations import _parse_status_line


def _term(registry, tid):
    return registry.terminal(tid)


def test_parse_connected_and_age(registry):
    line = "TERM|demo1|active|12|2|login on Demo"
    st = _parse_status_line(line, _term(registry, "demo1"))
    assert st is not None
    assert st.service_state == "active"
    assert st.connected is True
    assert st.log_age_seconds == 12
    assert st.healthy is True
    assert st.last_event == "login on Demo"


def test_parse_disconnected(registry):
    st = _parse_status_line("TERM|demo1|active|5|0|", _term(registry, "demo1"))
    assert st.connected is False
    assert st.healthy is False
    assert st.last_event is None


def test_parse_unknown_connection_when_estab_negative(registry):
    st = _parse_status_line("TERM|demo1|active|-1|-1|", _term(registry, "demo1"))
    assert st.connected is None
    assert st.log_age_seconds is None


def test_parse_rejects_malformed(registry):
    assert _parse_status_line("garbage", _term(registry, "demo1")) is None


async def test_status_marks_unreachable_host(registry, monkeypatch):
    async def boom(*a, **k):
        from mt4ctl.errors import RemoteCommandError

        raise RemoteCommandError("demo-box", 255, "unreachable")

    monkeypatch.setattr(ssh, "run", boom)
    rows = await operations.status(registry, ["demo1"])
    assert rows[0].service_state == "unknown"
    assert rows[0].last_event == "host unreachable"


async def test_control_live_requires_confirm(registry, monkeypatch):
    async def fail(*a, **k):
        raise AssertionError("ssh.run must not be called without confirmation")

    monkeypatch.setattr(ssh, "run", fail)
    with pytest.raises(ConfirmationRequiredError):
        await operations.control(registry, "live-main", "restart", confirm=False)


async def test_control_rejects_bad_action(registry):
    with pytest.raises(ValueError, match="action must be"):
        await operations.control(registry, "demo1", "frobnicate")


async def test_experts_parses_master_and_flags(registry, monkeypatch):
    from mt4ctl.ssh import CommandResult

    out = "MASTER|1\nEA|SQ-29-03-2026\\SQ AUDUSD H4 0.157419|343\nEA|Util\\Heartbeat|342\n"

    async def fake_run(host, script, **kw):
        return CommandResult(0, out, "")

    monkeypatch.setattr(ssh, "run", fake_run)
    r = await operations.experts(registry, "demo1")
    assert r.master is True
    assert [e.flags for e in r.experts] == [343, 342]
    assert r.experts[0].short_name == "SQ AUDUSD H4 0.157419"


async def test_experts_master_off_and_unknown(registry, monkeypatch):
    from mt4ctl.ssh import CommandResult

    async def fake_run(host, script, **kw):
        return CommandResult(0, "MASTER|0\n", "")

    monkeypatch.setattr(ssh, "run", fake_run)
    r = await operations.experts(registry, "demo1")
    assert r.master is False and r.experts == []


async def test_experts_unparsable_flags_and_unknown_master(registry, monkeypatch):
    from mt4ctl.ssh import CommandResult

    async def fake_run(host, script, **kw):
        return CommandResult(0, "MASTER|?\nEA|x\\y|notanumber\n", "")

    monkeypatch.setattr(ssh, "run", fake_run)
    r = await operations.experts(registry, "demo1")
    assert r.master is None
    assert r.experts[0].flags == -1
    assert r.experts[0].live_trading is None


async def test_experts_unreachable_host_does_not_raise(registry, monkeypatch):
    from mt4ctl.errors import RemoteCommandError

    async def boom(host, script, **kw):
        raise RemoteCommandError(host.id, 124, "command timed out")

    monkeypatch.setattr(ssh, "run", boom)
    r = await operations.experts(registry, "demo1")
    assert r.error is not None and r.experts == [] and r.master is None


async def test_info_nolog_and_unreachable(registry, monkeypatch):
    from mt4ctl.errors import RemoteCommandError
    from mt4ctl.ssh import CommandResult

    async def fake_run(host, script, **kw):
        return CommandResult(0, "INFO|nolog\n", "")

    monkeypatch.setattr(ssh, "run", fake_run)
    i = await operations.info(registry, "demo1")
    assert i.build is None and i.server is None and i.ping_ms is None and i.error is None

    async def boom(host, script, **kw):
        raise RemoteCommandError(host.id, 127, "could not start ssh")

    monkeypatch.setattr(ssh, "run", boom)
    i2 = await operations.info(registry, "demo1")
    assert i2.error is not None


async def test_info_parses_build_server_ping(registry, monkeypatch):
    from mt4ctl.ssh import CommandResult

    out = (
        "BUILD|Forex4you MT4 build 1470\n"
        "LOGIN|login on Darwinex-Demo through primary 2 (ping: 53.40 ms)\n"
    )

    async def fake_run(host, script, **kw):
        return CommandResult(0, out, "")

    monkeypatch.setattr(ssh, "run", fake_run)
    i = await operations.info(registry, "demo1")
    assert i.build == "Forex4you MT4 build 1470"
    assert i.server == "Darwinex-Demo"
    assert i.ping_ms == 53.40


async def test_logs_surfaces_ssh_failure(registry, monkeypatch):
    from mt4ctl.ssh import CommandResult

    async def fake_run(host, script, **kw):
        return CommandResult(255, "", "ssh: connect to host failed")

    monkeypatch.setattr(ssh, "run", fake_run)
    out = await operations.logs(registry, "demo1")
    assert "cannot read logs for demo1" in out
    assert "SSH to host" in out
    assert "(no output)" not in out
