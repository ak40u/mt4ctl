"""Status parsing, the live-confirmation guard, and deploy orchestration."""

from __future__ import annotations

import base64
import json

import pytest

from mt4ctl import operations, ssh
from mt4ctl.errors import ConfirmationRequiredError, DeployError
from mt4ctl.operations import _parse_status_line
from mt4ctl.ssh import CommandResult


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


# --------------------------------------------------------------------------- #
# Deploy orchestration
# --------------------------------------------------------------------------- #
def _make_bundle(tmp_path):
    """A minimal bundle: one chart -> one expert."""
    root = tmp_path / "bundle"
    (root / "profiles" / "default").mkdir(parents=True)
    (root / "MQL4" / "Experts" / "SQ").mkdir(parents=True)
    (root / "profiles" / "default" / "a.chr").write_text(
        "<chart>\r\n<expert>\r\nname=SQ\\Strat\r\n</expert>\r\nflags=343\r\n"
    )
    (root / "MQL4" / "Experts" / "SQ" / "Strat.ex4").write_bytes(b"EX4-binary")
    return root


def _manifest_b64(files: dict[str, str]) -> str:
    return base64.b64encode(json.dumps({"version": 1, "files": files}).encode()).decode()


class FakeSSH:
    """Records the order of remote calls and serves canned, per-step output."""

    def __init__(
        self,
        term_id,
        *,
        manifest="MISSING",
        dest=None,
        charts=None,
        user="trader",
        drain="ok",
        apply="ok",
        service="active",
        connected="2",
        log_text="",
    ):
        self.term_id = term_id
        self.manifest = manifest
        self.dest = dest or {}
        self.charts = charts or {}
        self.user = user
        self.drain = drain
        self.apply = apply
        self.service = service
        self.connected = connected
        self.log_text = log_text
        self.calls: list[str] = []
        self.put_calls: list[str] = []
        self.apply_scripts: list[str] = []
        self.manifest_put_scripts: list[str] = []

    def _classify(self, s: str) -> str:
        if "MWRESET" in s:
            return "mw_reset"
        if "ADOPT" in s:
            return "manifest_put"
        if "tar --version" in s:
            return "preflight"
        if 'mkdir "$LD"' in s:
            return "lock_acquire"
        if "notowner" in s:
            return "lock_release"
        if '"version":1' in s:
            return "apply"
        if 'echo "RESTORE' in s:
            return "restore"
        if 'echo "BACKUP' in s:
            return "backup"
        if 'echo "DRAIN' in s:
            return "drain"
        if 'echo "CURSOR' in s:
            return "cursor"
        if "tail -c +" in s:
            return "log_since"
        if "emit_dest" in s:
            return "remote_state"
        if "systemctl stop" in s:
            return "stop"
        if "systemctl start" in s:
            return "start"
        if "BROKER=" in s:
            return "status"
        if "no log files" in s:
            return "logs"
        return "other"

    def _remote_state_out(self) -> str:
        lines = [f"USER{operations.scripts.SEP}{self.user}", f"MANIFEST|{self.manifest}"]
        for rel, name in self.charts.items():
            lines.append(f"CHART|{rel}|{name}")
        for rel, h in self.dest.items():
            lines.append(f"DEST|{rel}|1|{h}" if h else f"DEST|{rel}|0|-")
        return "\n".join(lines) + "\n"

    async def run(self, host, script, **kw):
        label = self._classify(script)
        self.calls.append(label)
        if label == "apply":
            self.apply_scripts.append(script)
            if self.apply != "ok":
                from mt4ctl.errors import RemoteCommandError

                raise RemoteCommandError(host.id, 2, "apply failed")
        if label == "manifest_put":
            self.manifest_put_scripts.append(script)
        out = {
            "manifest_put": "ADOPT|ok\n",
            "lock_acquire": "LOCK|acquired\n",
            "lock_release": "LOCK|released\n",
            "preflight": "PRE|tar|ok\nPRE|sha|ok\nPRE|device|ok\n",
            "remote_state": self._remote_state_out(),
            "drain": f"DRAIN|{self.drain}\n",
            "backup": "BACKUP|/d/.mt4ctl/backups/ts.tar\n",
            "apply": "APPLY|ok\n",
            "restore": "RESTORE|done\n",
            "cursor": "CURSOR|/d/logs/20260523.log|100\n",
            "mw_reset": "MWRESET|2\n",
            "stop": "STATE|inactive|rc=0\n",
            "start": "STATE|active|rc=0\n",
            "status": f"TERM|{self.term_id}|{self.service}|5|{self.connected}|ok\n",
            "log_since": self.log_text,
            "logs": self.log_text,
        }.get(label, "")
        return CommandResult(0, out, "")

    async def put_tar(self, host, tar_bytes, dest, **kw):
        self.put_calls.append(dest)


def _install(monkeypatch, fake):
    monkeypatch.setattr(ssh, "run", fake.run)
    monkeypatch.setattr(ssh, "put_tar", fake.put_tar)


def _order(calls, *labels):
    """True if *labels* appear as an ordered subsequence of *calls*."""
    it = iter(calls)
    return all(any(c == lbl for c in it) for lbl in labels)


async def test_deploy_live_gate_no_remote_contact(registry, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no SSH before the live gate")

    monkeypatch.setattr(ssh, "run", boom)
    monkeypatch.setattr(ssh, "put_tar", boom)
    with pytest.raises(ConfirmationRequiredError):
        await operations.deploy(registry, "live-main", "/nonexistent", confirm=False)


async def test_deploy_dry_run_no_lock_no_mutation(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1")
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", _make_bundle(tmp_path), dry_run=True)
    assert result.dry_run is True
    assert set(result.plan.add) == {
        "MQL4/Experts/SQ/Strat.ex4",
        "profiles/default/a.chr",
    }
    assert fake.calls == ["remote_state"]  # no lock, stop, put_tar, apply
    assert fake.put_calls == []


async def test_deploy_happy_path_call_order(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", log_text="Experts\tStrat: loaded successfully\n")
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    # lock BEFORE state read; backup before upload; start before release
    assert _order(
        fake.calls,
        "lock_acquire",
        "remote_state",
        "stop",
        "drain",
        "backup",
        "apply",
        "start",
        "lock_release",
    )
    assert fake.put_calls and ".mt4ctl/staging/" in fake.put_calls[0]
    # put_tar happens between backup and apply
    assert fake.calls.index("backup") < fake.calls.index("apply")
    assert result.restarted is True
    assert result.verify_ok is True


async def test_deploy_empty_diff_skips_mutation_but_verifies(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    fake = FakeSSH(
        "demo1",
        manifest=_manifest_b64(files),
        dest=dict(files),  # every dest present with the bundle hash -> unchanged
        log_text="Experts\tStrat: loaded successfully\n",
    )
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", bundle)
    assert result.plan.has_changes is False
    assert "stop" not in fake.calls and "start" not in fake.calls
    assert "apply" not in fake.calls
    assert "lock_acquire" in fake.calls and "lock_release" in fake.calls
    assert result.verify_detail  # health still reported


async def test_deploy_drain_abort_does_not_apply_but_restarts(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", drain="timeout")
    _install(monkeypatch, fake)
    with pytest.raises(DeployError, match="did not exit"):
        await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert "apply" not in fake.calls
    assert "start" in fake.calls  # finally still brings it back
    assert "lock_release" in fake.calls


async def test_deploy_apply_failure_restores_before_start(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", apply="fail")
    _install(monkeypatch, fake)
    with pytest.raises(DeployError, match="restored"):
        await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert _order(fake.calls, "apply", "restore", "start", "lock_release")


async def test_deploy_pre_stop_failure_does_not_start(registry, monkeypatch, tmp_path):
    # an unmanaged-collision is detected pre-stop -> refuse before any mutation
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    fake = FakeSSH(
        "demo1", manifest="MISSING", dest=dict.fromkeys(files, "abc123")
    )  # present, unmanaged
    _install(monkeypatch, fake)
    with pytest.raises(DeployError, match="does not manage"):
        await operations.deploy(registry, "demo1", bundle)
    assert "stop" not in fake.calls
    assert "start" not in fake.calls
    assert "lock_release" in fake.calls  # lock still released


async def test_deploy_confirm_forwarded_to_control_on_live(registry, monkeypatch, tmp_path):
    fake = FakeSSH("live-main", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    # confirm=True must reach control's stop/start so the live-gate is not re-tripped
    result = await operations.deploy(
        registry, "live-main", _make_bundle(tmp_path), confirm=True
    )
    assert "stop" in fake.calls and "start" in fake.calls
    assert result.restarted is True


async def test_deploy_recompute_after_drain_reclassifies_chr(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    chr_rel = "profiles/default/a.chr"
    # First state read: manifest matches bundle for the .chr (would be "unchanged");
    # the post-drain re-read returns a DIFFERENT .chr hash (MT4 rewrote it on exit).
    seq = {"n": 0}
    fake = FakeSSH(
        "demo1",
        manifest=_manifest_b64(files),
        dest=dict(files),
        log_text="Experts\tStrat: loaded\n",
    )
    orig = fake._remote_state_out

    def staged_state():
        seq["n"] += 1
        if seq["n"] == 1:
            return orig()  # pre-stop: everything matches -> unchanged
        # post-drain: the chart's on-disk hash changed
        d = dict(files)
        d[chr_rel] = "rewritten_by_mt4_on_exit"
        return (
            "\n".join(
                ["USER|trader", f"MANIFEST|{_manifest_b64(files)}"]
                + [f"DEST|{r}|1|{h}" for r, h in d.items()]
            )
            + "\n"
        )

    fake._remote_state_out = staged_state
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", bundle)
    # the chart, "unchanged" pre-stop, is reclassified update after drain
    assert chr_rel in result.plan.update


async def test_deploy_verify_under_lock_release_after_verify(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    # the verify status read + log read must precede lock release
    last_status = max(i for i, c in enumerate(fake.calls) if c in ("status", "log_since"))
    assert last_status < fake.calls.index("lock_release")


async def test_deploy_verify_fails_when_ea_load_missing(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", log_text="nothing relevant in the log\n")
    _install(monkeypatch, fake)
    # verify_timeout=0 → single snapshot (no polling) so an unhealthy verify is
    # reported immediately instead of waiting out the poll window.
    result = await operations.deploy(
        registry, "demo1", _make_bundle(tmp_path), verify_timeout=0.0
    )
    assert result.verify_ok is False
    # progress is summarized by count, not a full not-yet-loaded dump.
    assert "0/1 experts loaded, 1 pending" in result.verify_detail


async def test_deploy_apply_runs_as_unit_user(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", user="pavel", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert fake.apply_scripts and "U='pavel'" in fake.apply_scripts[0]


async def test_deploy_verify_inconclusive_connection_not_failed(
    registry, monkeypatch, tmp_path
):
    # connected unknown (-1) must not flip verify to False on its own
    fake = FakeSSH("demo1", connected="-1", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert result.verify_ok is True
    assert "inconclusive" in result.verify_detail


# --------------------------------------------------------------------------- #
# verify polling + standalone verify
# --------------------------------------------------------------------------- #
def test_verify_attempts_bounds():
    assert operations._verify_attempts(0, 5) == 1  # opt out of polling
    assert operations._verify_attempts(-1, 5) == 1
    assert operations._verify_attempts(60, 0) == 1  # no interval -> single shot
    assert operations._verify_attempts(10, 5) == 3
    assert operations._verify_attempts(60, 5) == 13


async def test_verify_polled_retries_until_healthy(registry, monkeypatch):
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        return (calls["n"] >= 3, f"attempt {calls['n']}")

    slept: list[float] = []

    async def spy_sleep(s):
        slept.append(s)

    monkeypatch.setattr(operations, "_verify_snapshot", flaky)
    monkeypatch.setattr(operations.asyncio, "sleep", spy_sleep)
    term = registry.terminal("demo1")
    ok, detail = await operations._verify_polled(
        registry, term, registry.host_of(term), set(), None, timeout=60, interval=5
    )
    assert ok is True
    assert calls["n"] == 3  # stopped the moment it went healthy
    assert detail == "attempt 3"
    assert slept == [5, 5]  # slept between the three attempts, not after the last


async def test_verify_polled_times_out_reports_last_state(registry, monkeypatch):
    calls = {"n": 0}

    async def never(*a, **k):
        calls["n"] += 1
        return (False, "still down")

    async def nosleep(_s):
        pass

    monkeypatch.setattr(operations, "_verify_snapshot", never)
    monkeypatch.setattr(operations.asyncio, "sleep", nosleep)
    term = registry.terminal("demo1")
    ok, detail = await operations._verify_polled(
        registry, term, registry.host_of(term), set(), None, timeout=0.03, interval=0.01
    )
    assert ok is False
    assert detail == "still down"
    assert calls["n"] == operations._verify_attempts(0.03, 0.01)  # bounded, then gives up


async def test_deploy_verify_healthy_first_attempt_does_not_sleep(
    registry, monkeypatch, tmp_path
):
    fake = FakeSSH("demo1", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    slept: list[float] = []

    async def spy_sleep(s):
        slept.append(s)

    monkeypatch.setattr(operations.asyncio, "sleep", spy_sleep)
    # default poll window, but a healthy terminal returns on the first snapshot.
    result = await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert result.verify_ok is True
    assert slept == []


async def test_deploy_verify_polls_until_healthy(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    seq = {"n": 0}

    async def flaky(*a, **k):
        seq["n"] += 1
        return (seq["n"] >= 2, f"snap{seq['n']}")  # unhealthy once, then healthy

    slept: list[float] = []

    async def spy_sleep(s):
        slept.append(s)

    monkeypatch.setattr(operations, "_verify_snapshot", flaky)
    monkeypatch.setattr(operations.asyncio, "sleep", spy_sleep)
    result = await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert result.verify_ok is True
    assert result.verify_detail == "snap2"
    assert slept == [operations._VERIFY_INTERVAL_S]  # one retry between two snapshots


async def test_verify_op_reports_healthy_service_and_broker(registry, monkeypatch):
    fake = FakeSSH("demo1")  # service active, connected -> up
    _install(monkeypatch, fake)
    ok, detail = await operations.verify(registry, "demo1", timeout=0.0)
    assert ok is True
    assert "service=active" in detail and "broker connected" in detail
    # no expected EAs in a standalone verify -> no log read
    assert "log_since" not in fake.calls and "logs" not in fake.calls


async def test_verify_op_reports_unhealthy_when_broker_down(registry, monkeypatch):
    fake = FakeSSH("demo1", connected="0")  # service active but broker down
    _install(monkeypatch, fake)
    ok, detail = await operations.verify(registry, "demo1", timeout=0.0)
    assert ok is False
    assert "broker NOT connected" in detail


# --------------------------------------------------------------------------- #
# market-watch reset
# --------------------------------------------------------------------------- #
async def test_deploy_reset_market_watch_runs_in_stopped_window(
    registry, monkeypatch, tmp_path
):
    fake = FakeSSH("demo1", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    result = await operations.deploy(
        registry, "demo1", _make_bundle(tmp_path), reset_market_watch=True
    )
    # the symbols.sel delete must land between drain and the restart
    assert _order(fake.calls, "stop", "drain", "mw_reset", "start")
    assert result.market_watch_reset == 2


async def test_deploy_reset_only_no_changes_forces_stop_cycle(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    fake = FakeSSH(
        "demo1",
        manifest=_manifest_b64(files),
        dest=dict(files),  # everything matches -> no file changes
        log_text="Experts\tStrat: loaded\n",
    )
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", bundle, reset_market_watch=True)
    assert result.plan.has_changes is False
    assert "mw_reset" in fake.calls
    assert "stop" in fake.calls and "start" in fake.calls  # forced cycle for the reset
    assert "apply" not in fake.calls  # nothing to place
    assert result.market_watch_reset == 2


async def test_deploy_without_reset_leaves_market_watch(registry, monkeypatch, tmp_path):
    fake = FakeSSH("demo1", log_text="Experts\tStrat: loaded\n")
    _install(monkeypatch, fake)
    result = await operations.deploy(registry, "demo1", _make_bundle(tmp_path))
    assert "mw_reset" not in fake.calls
    assert result.market_watch_reset is None


# --------------------------------------------------------------------------- #
# status: connecting vs down
# --------------------------------------------------------------------------- #
def test_parse_active_enter_seconds(registry):
    st = _parse_status_line("TERM|demo1|active|5|0|login on Demo|42", _term(registry, "demo1"))
    assert st.active_enter_seconds == 42
    assert st.connected is False


def test_parse_active_enter_absent_when_field_missing(registry):
    # an older host emits the 6-field line; it must still parse (field optional)
    st = _parse_status_line("TERM|demo1|active|5|2|login on Demo", _term(registry, "demo1"))
    assert st.active_enter_seconds is None
    assert st.connected is True


def test_parse_active_enter_negative_is_none(registry):
    st = _parse_status_line("TERM|demo1|active|5|0||-1", _term(registry, "demo1"))
    assert st.active_enter_seconds is None


# --------------------------------------------------------------------------- #
# adopt — brownfield first cutover
# --------------------------------------------------------------------------- #
def _put_manifest_files(fake):
    """Decode the deployed.json the adopt run shipped to the host."""
    import base64

    script = fake.manifest_put_scripts[-1]
    token = script.split("echo ", 1)[1].split(" |", 1)[0]
    return json.loads(base64.b64decode(token))["files"]


async def test_adopt_live_gate_before_bundle_io(registry, monkeypatch):
    # gate must fire before read_bundle even with a nonexistent bundle path
    async def boom(*a, **k):
        raise AssertionError("no SSH before the live gate")

    monkeypatch.setattr(ssh, "run", boom)
    with pytest.raises(ConfirmationRequiredError):
        await operations.adopt(registry, "live-main", "/nonexistent", confirm=False)


async def test_adopt_happy_path_records_bundle_footprint(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    fake = FakeSSH("demo1", user="pavel", dest=dict(files))  # every dest present, hashes match
    _install(monkeypatch, fake)
    result = await operations.adopt(registry, "demo1", bundle)
    assert set(result.adopted) == set(files)
    assert result.drifted == ()
    assert result.foreign == ()  # no foreign charts reported by the host
    assert result.unit_user == "pavel"
    assert _order(fake.calls, "lock_acquire", "remote_state", "manifest_put", "lock_release")
    assert _put_manifest_files(fake) == files  # manifest = on-disk hashes


async def test_adopt_refuses_when_a_bundle_file_is_absent(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    present = dict(files)
    present.pop("MQL4/Experts/SQ/Strat.ex4")  # one dest missing on host
    fake = FakeSSH("demo1", dest=present)
    _install(monkeypatch, fake)
    with pytest.raises(DeployError, match="present and readable"):
        await operations.adopt(registry, "demo1", bundle)
    assert "manifest_put" not in fake.calls  # nothing written
    assert "lock_release" in fake.calls  # lock still released


async def test_adopt_releases_lock_when_state_read_raises(registry, monkeypatch, tmp_path):
    from mt4ctl.errors import RemoteCommandError

    bundle = _make_bundle(tmp_path)
    calls = []

    async def flaky_run(host, script, **kw):
        label = FakeSSH("demo1")._classify(script)
        calls.append(label)
        if label == "lock_acquire":
            return CommandResult(0, "LOCK|acquired\n", "")
        if label == "remote_state":
            raise RemoteCommandError(host.id, 255, "host blip")
        return CommandResult(0, "LOCK|released\n", "")

    monkeypatch.setattr(ssh, "run", flaky_run)
    with pytest.raises(RemoteCommandError):
        await operations.adopt(registry, "demo1", bundle)
    assert "lock_release" in calls  # finally released the lock despite the raise


async def test_adopt_reports_drift_for_present_but_differing_file(
    registry, monkeypatch, tmp_path
):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    on_host = dict(files)
    on_host["profiles/default/a.chr"] = "different_on_host"  # drift
    fake = FakeSSH("demo1", dest=on_host)
    _install(monkeypatch, fake)
    result = await operations.adopt(registry, "demo1", bundle)
    assert result.drifted == ("profiles/default/a.chr",)
    # the manifest records the ON-DISK hash, not the bundle hash
    assert _put_manifest_files(fake)["profiles/default/a.chr"] == "different_on_host"


async def test_adopt_is_bundle_scoped_watchdog_not_recorded(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    fake = FakeSSH(
        "demo1",
        dest=dict(files),
        charts={"profiles/default/watchdog.chr": "Watch\\Dog"},  # foreign chart live on host
    )
    _install(monkeypatch, fake)
    result = await operations.adopt(registry, "demo1", bundle)
    recorded = _put_manifest_files(fake)
    assert "profiles/default/watchdog.chr" not in recorded  # foreign file never adopted
    assert set(recorded) == set(files)
    # ...and it is reported as foreign so the operator sees it was left untouched
    assert result.foreign == ("profiles/default/watchdog.chr",)


async def test_adopt_live_requires_confirm_then_proceeds(registry, monkeypatch, tmp_path):
    bundle = _make_bundle(tmp_path)
    files, _ = operations.deploy_core.read_bundle(bundle)
    fake = FakeSSH("live-main", dest=dict(files))
    _install(monkeypatch, fake)
    result = await operations.adopt(registry, "live-main", bundle, confirm=True)
    assert set(result.adopted) == set(files)
