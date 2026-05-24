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


def test_init_creates_private_file(monkeypatch, tmp_path):
    import os
    import stat

    if os.name != "posix":
        pytest.skip("POSIX permission check")
    target = tmp_path / "terminals.yaml"
    monkeypatch.setattr("sys.argv", ["mt4ctl", "init", str(target)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_deploy_parses_args_and_calls_operation(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.models import DeployPlan, DeployResult

    captured = {}

    async def fake_deploy(
        registry,
        terminal,
        bundle,
        *,
        dry_run,
        confirm,
        reset_market_watch,
        verify_timeout,
    ):
        captured.update(
            terminal=terminal,
            bundle=bundle,
            dry_run=dry_run,
            confirm=confirm,
            reset_market_watch=reset_market_watch,
            verify_timeout=verify_timeout,
        )
        return DeployResult(
            terminal=terminal,
            plan=DeployPlan(add=("MQL4/Experts/A.ex4",)),
            backup_path=None,
            restarted=False,
            verify_ok=False,
            verify_detail="dry-run (nothing applied)",
            dry_run=True,
        )

    monkeypatch.setattr(operations, "deploy", fake_deploy)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mt4ctl",
            "deploy",
            "t1",
            "/path/bundle",
            "--dry-run",
            "--confirm",
            "--reset-market-watch",
            "--verify-timeout",
            "30",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {
        "terminal": "t1",
        "bundle": "/path/bundle",
        "dry_run": True,
        "confirm": True,
        "reset_market_watch": True,
        "verify_timeout": 30.0,
    }
    assert "plan: +1" in capsys.readouterr().out


def test_deploy_reports_error_with_exit_1(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.errors import BundleError

    async def boom(registry, terminal, bundle, *, dry_run, confirm, **_kw):
        raise BundleError("bundle directory not found: '/nope'")

    monkeypatch.setattr(operations, "deploy", boom)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "deploy", "t1", "/nope"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "bundle directory not found" in capsys.readouterr().err


def test_deploy_missing_args_errors(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["mt4ctl", "deploy", "t1"])  # missing bundle
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # argparse usage error


def test_verify_parses_args_and_exit_code_reflects_health(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations

    captured = {}

    async def fake_verify(registry, terminal, *, timeout):
        captured.update(terminal=terminal, timeout=timeout)
        return (False, "service=active; broker NOT connected")

    monkeypatch.setattr(operations, "verify", fake_verify)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "verify", "t1", "--timeout", "30"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # not healthy -> non-zero
    assert captured == {"terminal": "t1", "timeout": 30.0}
    assert "NOT healthy" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# CLI/MCP parity: read + control commands
# --------------------------------------------------------------------------- #
def _ts(monkeypatch, **kw):
    from mt4ctl.models import Env, TerminalStatus

    fields = {
        "id": "t1",
        "host": "box",
        "env": Env.DEMO,
        "account": "1",
        "service_state": "active",
        "connected": True,
        "log_age_seconds": 3,
        **kw,
    }
    return TerminalStatus(**fields)


def test_status_exit_zero_when_all_healthy(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations

    captured = {}

    async def fake_status(registry, ids):
        captured["ids"] = ids
        return [_ts(monkeypatch)]

    monkeypatch.setattr(operations, "status", fake_status)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "status"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured["ids"] is None  # bare "status" -> all
    assert "t1" in capsys.readouterr().out


def test_status_exit_one_when_any_unhealthy(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations

    captured = {}

    async def fake_status(registry, ids):
        captured["ids"] = ids
        return [_ts(monkeypatch, connected=False)]

    monkeypatch.setattr(operations, "status", fake_status)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "status", "t1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # scriptable: unhealthy -> non-zero exit
    assert captured["ids"] == ["t1"]


def test_logs_forwards_args(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations

    captured = {}

    async def fake_logs(registry, terminal, *, pattern, lines):
        captured.update(terminal=terminal, pattern=pattern, lines=lines)
        return "# /d/logs/x.log\nlogin on Demo"

    monkeypatch.setattr(operations, "logs", fake_logs)
    monkeypatch.setattr(
        "sys.argv", ["mt4ctl", "logs", "t1", "--pattern", "login", "--lines", "10"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {"terminal": "t1", "pattern": "login", "lines": 10}
    assert "login on Demo" in capsys.readouterr().out


def test_logs_exit_one_on_error_output(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations

    async def fake_logs(registry, terminal, *, pattern, lines):
        return "error: cannot read logs for t1: SSH to host box failed"

    monkeypatch.setattr(operations, "logs", fake_logs)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "logs", "t1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


def test_control_forwards_and_formats(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations

    captured = {}

    async def fake_control(registry, terminal, action, *, confirm):
        captured.update(terminal=terminal, action=action, confirm=confirm)
        return _ts(monkeypatch)

    monkeypatch.setattr(operations, "control", fake_control)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "control", "t1", "restart", "--confirm"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {"terminal": "t1", "action": "restart", "confirm": True}
    assert "t1: restart done -> service=active, conn=up" in capsys.readouterr().out


def test_control_rejects_bad_action(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["mt4ctl", "control", "t1", "frobnicate"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # argparse choices reject it


def test_ea_list_single_lists_names(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.models import Expert, ExpertsReport

    captured = {}

    async def fake_experts_all(registry, ids):
        captured["ids"] = ids
        return [ExpertsReport(terminal="t1", master=True, experts=[Expert("SQ\\Strat", 343)])]

    monkeypatch.setattr(operations, "experts_all", fake_experts_all)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "ea-list", "t1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured["ids"] == ["t1"]
    assert "Strat" in capsys.readouterr().out


def test_ea_list_all_uses_registry_terminals(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.models import ExpertsReport

    captured = {}

    async def fake_experts_all(registry, ids):
        captured["ids"] = ids
        return [ExpertsReport(terminal=t, master=True, experts=[]) for t in ids]

    monkeypatch.setattr(operations, "experts_all", fake_experts_all)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "ea-list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured["ids"] == ["t1"]  # "all" expands to the registry's terminals


def test_autotrading_renders_table(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.models import ExpertsReport

    async def fake_experts_all(registry, ids):
        return [ExpertsReport(terminal="t1", master=True, experts=[])]

    monkeypatch.setattr(operations, "experts_all", fake_experts_all)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "autotrading", "t1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "AUTOTRADING" in capsys.readouterr().out


def test_info_renders_table(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.models import TerminalInfo

    captured = {}

    async def fake_info_all(registry, ids):
        captured["ids"] = ids
        return [TerminalInfo(terminal="t1", build="build 1470", server="Demo", ping_ms=50.0)]

    monkeypatch.setattr(operations, "info_all", fake_info_all)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "info", "t1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured["ids"] == ["t1"]
    assert "build 1470" in capsys.readouterr().out


def test_screenshot_prints_saved_path(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from pathlib import Path

    from mt4ctl import operations

    captured = {}

    async def fake_screenshot(registry, terminal, *, out_dir):
        captured.update(terminal=terminal, out_dir=str(out_dir))
        return Path("/tmp/shot/t1.png")

    monkeypatch.setattr(operations, "screenshot", fake_screenshot)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "screenshot", "t1", "--out-dir", "/tmp/shot"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {"terminal": "t1", "out_dir": "/tmp/shot"}
    assert "saved /tmp/shot/t1.png" in capsys.readouterr().out


def test_login_forwards_args(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import login as login_mod

    captured = {}

    async def fake_login(registry, terminal, *, account, server, password, confirm):
        captured.update(
            terminal=terminal,
            account=account,
            server=server,
            password=password,
            confirm=confirm,
        )
        return "t1: login OK"

    monkeypatch.setattr(login_mod, "login", fake_login)
    monkeypatch.setattr(
        "sys.argv",
        ["mt4ctl", "login", "t1", "Broker-Demo", "--account", "123", "--confirm"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {
        "terminal": "t1",
        "account": "123",
        "server": "Broker-Demo",
        "password": None,
        "confirm": True,
    }
    assert "login OK" in capsys.readouterr().out


def test_adopt_parses_args_and_calls_operation(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.models import AdoptResult

    captured = {}

    async def fake_adopt(registry, terminal, bundle, *, confirm):
        captured.update(terminal=terminal, bundle=bundle, confirm=confirm)
        return AdoptResult(
            terminal=terminal,
            adopted=("MQL4/Experts/A.ex4",),
            drifted=(),
            foreign=(),
            unit_user="trader",
            manifest_path="/d/.mt4ctl/deployed.json",
        )

    monkeypatch.setattr(operations, "adopt", fake_adopt)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "adopt", "t1", "/path/bundle", "--confirm"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {"terminal": "t1", "bundle": "/path/bundle", "confirm": True}
    assert "adopted 1 files" in capsys.readouterr().out


def test_adopt_rejects_dry_run_flag(monkeypatch, tmp_path):
    # adopt has no preview mode — --dry-run must not be accepted
    monkeypatch.setattr("sys.argv", ["mt4ctl", "adopt", "t1", "/b", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # argparse rejects unknown flag


def test_adopt_reports_error_with_exit_1(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.errors import DeployError

    async def boom(registry, terminal, bundle, *, confirm):
        raise DeployError(
            "adopt requires every bundle file present on the host; absent: x.ex4"
        )

    monkeypatch.setattr(operations, "adopt", boom)
    monkeypatch.setattr("sys.argv", ["mt4ctl", "adopt", "t1", "/b"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "absent" in capsys.readouterr().err


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
