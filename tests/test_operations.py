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
