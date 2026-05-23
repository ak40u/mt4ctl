"""Pure server-layer logic: status formatting and the error guard."""

from __future__ import annotations

import pytest

from mt4ctl.errors import Mt4ctlError
from mt4ctl.models import AdoptResult, DeployPlan, DeployResult, Env, TerminalStatus
from mt4ctl.server import (
    _fmt_adopt_result,
    _fmt_deploy_plan,
    _fmt_deploy_result,
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


# --------------------------------------------------------------------------- #
# adopt formatter + tool
# --------------------------------------------------------------------------- #
def test_fmt_adopt_result_clean():
    res = AdoptResult(
        terminal="demo3",
        adopted=("MQL4/Experts/A.ex4", "profiles/default/a.chr"),
        drifted=(),
        unit_user="pavel",
        manifest_path="/d/.mt4ctl/deployed.json",
    )
    out = _fmt_adopt_result(res)
    assert "demo3: adopted 2 files (owner pavel)" in out
    assert "/d/.mt4ctl/deployed.json" in out
    assert "--dry-run" in out  # next-step points at deploy dry-run
    assert "differs" not in out  # no drift section when none


def test_fmt_adopt_result_drift():
    res = AdoptResult(
        terminal="demo3",
        adopted=("profiles/default/a.chr",),
        drifted=("profiles/default/a.chr",),
        unit_user="pavel",
        manifest_path="/d/.mt4ctl/deployed.json",
    )
    out = _fmt_adopt_result(res)
    assert "differs from the bundle" in out
    assert "profiles/default/a.chr" in out


async def test_mt4_adopt_is_registered_without_dry_run():
    from mt4ctl.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert "mt4_adopt" in tools
    schema = tools["mt4_adopt"].inputSchema
    assert "dry_run" not in schema.get("properties", {})  # adopt has no preview mode
    assert set(schema.get("properties", {})) >= {"terminal", "bundle", "confirm"}
