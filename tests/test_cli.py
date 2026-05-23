"""CLI dispatch and the offline subcommands."""

from __future__ import annotations

import pytest

from mt4ctl import cli, diagnostics

VALID_YAML = """
hosts:
  box: {ssh: box-alias}
terminals:
  t1: {host: box, service: mt4-t1, data_dir: /home/trader/t1, account: "1000001"}
"""


def _use_registry(monkeypatch, tmp_path, text=VALID_YAML):
    f = tmp_path / "terminals.yaml"
    f.write_text(text)
    monkeypatch.setenv("MT4CTL_CONFIG", str(f))
    return f


def test_version(monkeypatch, capsys):
    from mt4ctl import __version__

    monkeypatch.setattr("sys.argv", ["mt4ctl", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_on_tty_explains_and_exits(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mt4ctl"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "MCP stdio server" in capsys.readouterr().err


def test_list_offline(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "t1" in out and "box" in out and "1000001" in out


def test_list_reports_config_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MT4CTL_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr("sys.argv", ["mt4ctl", "list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "MT4CTL_CONFIG is set to" in capsys.readouterr().err


def test_init_writes_starter_and_refuses_overwrite(monkeypatch, tmp_path, capsys):
    target = tmp_path / "terminals.yaml"
    monkeypatch.setattr("sys.argv", ["mt4ctl", "init", str(target)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert target.is_file() and "hosts:" in target.read_text()

    # second run refuses to clobber
    monkeypatch.setattr("sys.argv", ["mt4ctl", "init", str(target)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err


def test_doctor_runs_and_sets_exit_code(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)

    async def fake_diag(registry):
        return [diagnostics.Check("host box", "fail", "unreachable")]

    monkeypatch.setattr(diagnostics, "run_diagnostics", fake_diag)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "doctor"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # a failed check -> non-zero
    assert "host box" in capsys.readouterr().out
