"""Pure server-layer logic: status formatting and the error guard."""

from __future__ import annotations

import pytest

from mt4ctl.errors import Mt4ctlError
from mt4ctl.models import (
    AdoptResult,
    DeployPlan,
    DeployResult,
    Env,
    Expert,
    ExpertsReport,
    TerminalInfo,
    TerminalStatus,
)
from mt4ctl.server import (
    _fmt_adopt_result,
    _fmt_autotrading,
    _fmt_control,
    _fmt_deploy_plan,
    _fmt_deploy_result,
    _fmt_experts,
    _fmt_info,
    _fmt_status,
    _guard,
)


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


def test_fmt_status_gives_reason_and_next_step_for_disconnected():
    out = _fmt_status([_status(id="demo1", service_state="active", connected=False)])
    assert "no broker connection" in out  # per-row reason
    assert "next steps:" in out  # actionable footer
    assert "mt4_login" in out


def test_fmt_status_no_next_steps_when_all_healthy():
    out = _fmt_status([_status(connected=True)])
    assert "next steps:" not in out


def test_fmt_status_unknown_connection_renders_question_mark():
    out = _fmt_status([_status(connected=None, log_age_seconds=None)])
    assert "?" in out
    assert " - " in out  # missing log age shown as '-'


def test_fmt_status_recent_restart_reads_as_connecting_not_login():
    # disconnected but only seconds since the unit became active -> still reconnecting
    out = _fmt_status(
        [_status(id="demo1", service_state="active", connected=False, active_enter_seconds=8)]
    )
    assert "connecting" in out
    assert "8s since restart" in out
    assert "mt4_login" not in out  # do NOT tell the operator to re-login mid-reconnect


def test_fmt_status_long_uptime_disconnect_is_persistent_down():
    out = _fmt_status(
        [
            _status(
                id="demo1", service_state="active", connected=False, active_enter_seconds=9999
            )
        ]
    )
    assert "no broker connection" in out
    assert "mt4_login" in out  # genuinely down -> login is the right hint


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


# --------------------------------------------------------------------------- #
# Deploy formatters + tool registration
# --------------------------------------------------------------------------- #
def test_fmt_deploy_plan_lists_changes_and_foreign():
    plan = DeployPlan(
        add=("MQL4/Experts/A.ex4",),
        update=("profiles/default/a.chr",),
        unchanged=("MQL4/Experts/B.ex4",),
        foreign=("profiles/default/watchdog.chr",),
        notes=("drift: a.chr changed on host",),
    )
    out = _fmt_deploy_plan(plan)
    assert "plan: +1 ~1 -0" in out
    assert "MQL4/Experts/A.ex4" in out
    assert "unchanged: 1" in out
    assert "watchdog.chr" in out and "left untouched" in out
    assert "drift" in out


def test_fmt_deploy_plan_no_changes():
    assert "no changes" in _fmt_deploy_plan(DeployPlan())


def test_fmt_deploy_plan_conflicts_refused():
    out = _fmt_deploy_plan(DeployPlan(conflicts=("MQL4/Experts/X.ex4",)))
    assert "REFUSED" in out
    assert "MQL4/Experts/X.ex4" in out
    assert "drop them from the bundle" in out
    assert "renumber" not in out  # an .ex4 conflict is not a chart-slot collision


def test_fmt_deploy_plan_chart_conflict_suggests_renumber():
    out = _fmt_deploy_plan(DeployPlan(conflicts=("profiles/default/chart72.chr",)))
    assert "REFUSED" in out
    assert "renumber" in out  # actionable foreign-chart-slot guidance


def test_fmt_deploy_plan_reset_market_watch_note_in_dry_run():
    out = _fmt_deploy_plan(DeployPlan(), reset_market_watch=True)
    assert "--reset-market-watch" in out
    assert "symbols.sel" in out


def test_fmt_deploy_plan_chr_update_carries_drift_note():
    out = _fmt_deploy_plan(DeployPlan(update=("profiles/default/a.chr",)))
    assert "cosmetic drift" in out  # heads-up on post-restart .chr churn


def test_fmt_deploy_result_market_watch_reset_line():
    res = DeployResult(
        terminal="demo1",
        plan=DeployPlan(),
        backup_path=None,
        restarted=True,
        verify_ok=True,
        verify_detail="service=active; broker connected",
        market_watch_reset=2,
    )
    out = _fmt_deploy_result(res)
    assert "market watch: reset (2 symbols.sel removed" in out


def test_fmt_deploy_result_no_market_watch_line_when_not_requested():
    res = DeployResult(
        terminal="demo1",
        plan=DeployPlan(add=("MQL4/Experts/A.ex4",)),
        backup_path=None,
        restarted=True,
        verify_ok=True,
        verify_detail="ok",
    )
    assert "market watch" not in _fmt_deploy_result(res)


def test_fmt_deploy_result_ok():
    res = DeployResult(
        terminal="demo1",
        plan=DeployPlan(add=("MQL4/Experts/A.ex4",)),
        backup_path="/d/.mt4ctl/backups/ts.tar",
        restarted=True,
        verify_ok=True,
        verify_detail="service=active; broker connected; 1 experts loaded",
    )
    out = _fmt_deploy_result(res)
    assert "demo1: deployed +1" in out
    assert "backup: /d/.mt4ctl/backups/ts.tar" in out
    assert "restarted: yes" in out
    assert "verify: ok" in out


def test_fmt_deploy_result_verify_failed_is_report_only():
    res = DeployResult(
        terminal="demo1",
        plan=DeployPlan(update=("MQL4/Experts/A.ex4",)),
        backup_path=None,
        restarted=True,
        verify_ok=False,
        verify_detail="service=active; broker connected; EA load not confirmed: Strat",
    )
    out = _fmt_deploy_result(res)
    assert "verify: NOT confirmed" in out
    assert "did NOT revert" in out  # report-only is explicit
    assert "re-deploy the previous bundle" in out


def test_fmt_deploy_result_inconclusive_connection():
    res = DeployResult(
        terminal="demo1",
        plan=DeployPlan(),
        backup_path=None,
        restarted=False,
        verify_ok=True,
        verify_detail="service=active; broker inconclusive",
    )
    assert "inconclusive" in _fmt_deploy_result(res)


async def test_mt4_deploy_is_registered():
    from mt4ctl.server import mcp

    tools = {t.name for t in await mcp.list_tools()}
    assert "mt4_deploy" in tools


async def test_mt4_verify_is_registered():
    from mt4ctl.server import mcp

    tools = {t.name for t in await mcp.list_tools()}
    assert "mt4_verify" in tools


# --------------------------------------------------------------------------- #
# Shared formatters reused by both the MCP tools and the CLI
# --------------------------------------------------------------------------- #
def test_fmt_control_renders_conn():
    st = _status(service_state="active", connected=True)
    assert _fmt_control("t1", "restart", st) == "t1: restart done -> service=active, conn=up"
    st_down = _status(service_state="active", connected=False)
    assert "conn=down" in _fmt_control("t1", "start", st_down)
    st_unknown = _status(service_state="active", connected=None)
    assert "conn=?" in _fmt_control("t1", "stop", st_unknown)


def test_fmt_experts_single_lists_names():
    r = ExpertsReport(terminal="t1", master=True, experts=[Expert("SQ\\Strat", 343)])
    out = _fmt_experts(["t1"], [r])
    assert "t1: 1 experts" in out and "Strat" in out


def test_fmt_experts_single_none_and_unreachable():
    assert "(none)" in _fmt_experts(["t1"], [ExpertsReport("t1", True, [])])
    err = ExpertsReport("t1", None, [], error="boom")
    assert "unreachable — boom" in _fmt_experts(["t1"], [err])


def test_fmt_experts_many_counts():
    reps = [
        ExpertsReport("t1", True, [Expert("a", 1), Expert("b", 1)]),
        ExpertsReport("t2", None, [], error="down"),
    ]
    out = _fmt_experts(["t1", "t2"], reps)
    assert "t1" in out and "2 experts" in out
    assert "t2" in out and "unreachable — down" in out


def test_fmt_autotrading_master_off_and_live_off():
    off = ExpertsReport("t1", False, [Expert("x", 343)])
    assert "master AutoTrading OFF" in _fmt_autotrading([off])
    some_off = ExpertsReport("t2", True, [Expert("a", 343), Expert("b", 342)])
    out = _fmt_autotrading([some_off])
    assert "live-trading off" in out  # flags=342 -> live bit clear
    unreachable = ExpertsReport("t3", None, [], error="x")
    assert "unreachable" in _fmt_autotrading([unreachable])


def test_fmt_info_normal_and_unreachable():
    ok = TerminalInfo(terminal="t1", build="build 1470", server="Demo", ping_ms=53.0)
    out = _fmt_info([ok])
    assert "build 1470" in out and "Demo" in out and "53ms" in out
    bad = TerminalInfo(terminal="t2", build=None, server=None, ping_ms=None, error="boom")
    assert "unreachable — boom" in _fmt_info([bad])


# --------------------------------------------------------------------------- #
# adopt formatter + tool
# --------------------------------------------------------------------------- #
def test_fmt_adopt_result_clean():
    res = AdoptResult(
        terminal="demo3",
        adopted=("MQL4/Experts/A.ex4", "profiles/default/a.chr"),
        drifted=(),
        foreign=(),
        unit_user="pavel",
        manifest_path="/d/.mt4ctl/deployed.json",
    )
    out = _fmt_adopt_result(res)
    assert "demo3: adopted 2 files (owner pavel)" in out
    assert "/d/.mt4ctl/deployed.json" in out
    assert "--dry-run" in out  # next-step points at deploy dry-run
    assert "differs" not in out  # no drift section when none
    assert "left foreign" not in out  # no foreign section when none


def test_fmt_adopt_result_drift():
    res = AdoptResult(
        terminal="demo3",
        adopted=("profiles/default/a.chr",),
        drifted=("profiles/default/a.chr",),
        foreign=(),
        unit_user="pavel",
        manifest_path="/d/.mt4ctl/deployed.json",
    )
    out = _fmt_adopt_result(res)
    assert "differs from the bundle" in out
    assert "profiles/default/a.chr" in out


def test_fmt_adopt_result_reports_foreign_charts():
    res = AdoptResult(
        terminal="demo3",
        adopted=("profiles/default/a.chr",),
        drifted=(),
        foreign=("profiles/default/watchdog.chr",),
        unit_user="pavel",
        manifest_path="/d/.mt4ctl/deployed.json",
    )
    out = _fmt_adopt_result(res)
    assert "left foreign" in out and "untouched" in out
    assert "profiles/default/watchdog.chr" in out


async def test_mt4_adopt_is_registered_without_dry_run():
    from mt4ctl.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert "mt4_adopt" in tools
    schema = tools["mt4_adopt"].inputSchema
    assert "dry_run" not in schema.get("properties", {})  # adopt has no preview mode
    assert set(schema.get("properties", {})) >= {"terminal", "bundle", "confirm"}
