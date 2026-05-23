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

    async def fake_deploy(registry, terminal, bundle, *, dry_run, confirm):
        captured.update(terminal=terminal, bundle=bundle, dry_run=dry_run, confirm=confirm)
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
        "sys.argv", ["mt4ctl", "deploy", "t1", "/path/bundle", "--dry-run", "--confirm"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert captured == {
        "terminal": "t1",
        "bundle": "/path/bundle",
        "dry_run": True,
        "confirm": True,
    }
    assert "plan: +1" in capsys.readouterr().out


def test_deploy_reports_error_with_exit_1(monkeypatch, tmp_path, capsys):
    _use_registry(monkeypatch, tmp_path)
    from mt4ctl import operations
    from mt4ctl.errors import BundleError

    async def boom(registry, terminal, bundle, *, dry_run, confirm):
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
