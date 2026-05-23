"""Pure server-layer logic: status formatting and the error guard."""

from __future__ import annotations

import pytest

from mt4ctl import server
from mt4ctl.errors import Mt4ctlError
from mt4ctl.models import Env, TerminalStatus
from mt4ctl.server import _fmt_status, _guard


def _status(**kw):
    fields = {
        "id": "t",
        "host": "h",
        "env": Env.DEMO,
        "account": "1",
        "service_state": "active",
        "connected": True,
        "log_age_seconds": 5,
        **kw,
    }
    return TerminalStatus(**fields)


def test_fmt_status_empty():
    assert _fmt_status([]) == "no terminals match."


def test_fmt_status_renders_health_and_flag():
    rows = [
        _status(id="ok", connected=True),
        _status(id="bad", connected=False),
    ]
    out = _fmt_status(rows)
    assert "ok" in out and "bad" in out
    assert "up" in out and "down" in out
    # unhealthy rows carry a detail marker, healthy ones do not
    bad_line = next(ln for ln in out.splitlines() if ln.startswith("bad"))
    ok_line = next(ln for ln in out.splitlines() if ln.startswith("ok"))
    assert "<- " in bad_line
    assert "<- " not in ok_line


def test_fmt_status_surfaces_unreachable_cause():
    out = _fmt_status(
        [_status(service_state="unknown", connected=None, last_event="host unreachable")]
    )
    assert "host unreachable" in out


def test_fmt_status_unknown_connection_renders_question_mark():
    out = _fmt_status([_status(connected=None, log_age_seconds=None)])
    assert "?" in out
    assert " - " in out  # missing log age shown as '-'


async def test_guard_converts_known_errors_to_message():
    @_guard
    async def boom() -> str:
        raise Mt4ctlError("nope")

    assert await boom() == "error: nope"


async def test_guard_converts_value_error():
    @_guard
    async def boom() -> str:
        raise ValueError("bad arg")

    assert await boom() == "error: bad arg"


async def test_guard_passes_through_success():
    @_guard
    async def fine() -> str:
        return "ok"

    assert await fine() == "ok"


async def test_guard_does_not_swallow_unexpected_errors():
    @_guard
    async def boom() -> str:
        raise RuntimeError("real bug")

    with pytest.raises(RuntimeError):
        await boom()


def test_main_version(monkeypatch, capsys):
    from mt4ctl import __version__

    monkeypatch.setattr("sys.argv", ["mt4ctl", "--version"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_tty_explains_instead_of_hanging(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mt4ctl"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def boom() -> None:
        raise AssertionError("server must not start on a TTY")

    monkeypatch.setattr(server.mcp, "run", boom)
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    assert "MCP stdio server" in capsys.readouterr().err
