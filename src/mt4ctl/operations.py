"""High-level terminal operations: status, logs, screenshots, lifecycle control.

These functions take a :class:`~mt4ctl.models.Registry`, talk to hosts through
:mod:`mt4ctl.ssh`, and return domain objects or plain strings. The MCP server in
:mod:`mt4ctl.server` is a thin shell over this module.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import socket
import sys
import tarfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

from . import deploy as deploy_core
from . import scripts, ssh
from .errors import ConfirmationRequiredError, DeployError, RemoteCommandError
from .models import (
    AdoptResult,
    DeployPlan,
    DeployResult,
    Env,
    Expert,
    ExpertsReport,
    Host,
    Registry,
    Terminal,
    TerminalInfo,
    TerminalStatus,
)

_PING_RE = re.compile(r"login on (\S+) through[^(]*\(ping: ([0-9.]+) ms\)")

CONTROL_ACTIONS = ("start", "stop", "restart")


def _parse_status_line(line: str, term: Terminal) -> TerminalStatus | None:
    parts = line.split(scripts.SEP)
    if len(parts) < 6 or parts[0] != "TERM":
        return None
    _, _id, state, age_s, estab_s, last = parts[:6]
    try:
        age = int(age_s)
    except ValueError:
        age = -1
    try:
        estab = int(estab_s)
    except ValueError:
        estab = -1
    return TerminalStatus(
        id=term.id,
        host=term.host,
        env=term.env,
        account=term.account,
        service_state=state or "unknown",
        connected=(estab > 0) if estab >= 0 else None,
        log_age_seconds=age if age >= 0 else None,
        last_event=last.strip() or None,
    )


async def _status_for_host(host: Host, terms: list[Terminal]) -> dict[str, TerminalStatus]:
    specs = [(t.id, t.service, t.data_dir) for t in terms]
    script = scripts.build_status_script(host.broker_host, specs)
    by_id = {t.id: t for t in terms}
    out: dict[str, TerminalStatus] = {}
    try:
        result = await ssh.run(host, script, timeout=30.0, check=False)
        for line in result.stdout.splitlines():
            if line.startswith("TERM" + scripts.SEP):
                tid = line.split(scripts.SEP)[1]
                if (term := by_id.get(tid)) and (st := _parse_status_line(line, term)):
                    out[tid] = st
    except RemoteCommandError:
        pass  # unreachable host -> emit 'unknown' below
    for term in terms:
        out.setdefault(
            term.id,
            TerminalStatus(
                id=term.id,
                host=term.host,
                env=term.env,
                account=term.account,
                service_state="unknown",
                connected=None,
                log_age_seconds=None,
                last_event="host unreachable",
            ),
        )
    return out


async def status(
    registry: Registry, terminal_ids: list[str] | None = None
) -> list[TerminalStatus]:
    """Resolve status for the given terminals (all of them when *terminal_ids* is None).

    Hosts are queried concurrently; results preserve registry order.
    """
    selected = (
        [registry.terminal(t) for t in terminal_ids]
        if terminal_ids
        else list(registry.terminals.values())
    )
    grouped: dict[str, list[Terminal]] = {}
    for term in selected:
        grouped.setdefault(term.host, []).append(term)

    results = await asyncio.gather(
        *(_status_for_host(registry.host(hid), terms) for hid, terms in grouped.items())
    )
    merged: dict[str, TerminalStatus] = {}
    for chunk in results:
        merged.update(chunk)
    return [merged[t.id] for t in selected]


async def logs(
    registry: Registry,
    terminal_id: str,
    *,
    pattern: str | None = None,
    lines: int = 50,
) -> str:
    """Return the tail of a terminal's newest log file, optionally grep-filtered."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    script = scripts.build_logs_script(term.data_dir, pattern, max(1, min(lines, 1000)))
    result = await ssh.run(host, script, timeout=30.0, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or "host unreachable"
        return (
            f"error: cannot read logs for {terminal_id}: SSH to host {host.id} "
            f"failed (exit {result.returncode}): {detail}. Check the SSH alias, key, "
            f"and BatchMode access, then retry."
        )
    return result.stdout.strip() or "(no log output)"


async def experts(registry: Registry, terminal_id: str) -> ExpertsReport:
    """Report the AutoTrading master switch and attached experts for a terminal."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    try:
        result = await ssh.run(host, scripts.build_experts_script(term.data_dir), check=False)
    except RemoteCommandError as exc:
        # One unreachable host must not blank a whole fan-out (matches status()).
        return ExpertsReport(terminal=terminal_id, master=None, experts=[], error=str(exc))
    master: bool | None = None
    eas: list[Expert] = []
    for line in result.stdout.splitlines():
        parts = line.split(scripts.SEP)
        if parts[0] == "MASTER" and len(parts) >= 2:
            master = {"1": True, "0": False}.get(parts[1].strip())
        elif parts[0] == "EA" and len(parts) >= 3:
            try:
                flags = int(parts[2])
            except ValueError:
                flags = -1
            eas.append(Expert(name=parts[1], flags=flags))
    return ExpertsReport(terminal=terminal_id, master=master, experts=eas)


async def info(registry: Registry, terminal_id: str) -> TerminalInfo:
    """Report build / broker server / last ping for a terminal, parsed from logs."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    try:
        result = await ssh.run(host, scripts.build_info_script(term.data_dir), check=False)
    except RemoteCommandError as exc:
        return TerminalInfo(
            terminal=terminal_id, build=None, server=None, ping_ms=None, error=str(exc)
        )
    build = server = None
    ping: float | None = None
    for line in result.stdout.splitlines():
        parts = line.split(scripts.SEP, 1)
        if parts[0] == "BUILD" and len(parts) == 2:
            build = parts[1].strip() or None
        elif parts[0] == "LOGIN" and len(parts) == 2 and (m := _PING_RE.search(parts[1])):
            server = m.group(1)
            ping = float(m.group(2))
    return TerminalInfo(terminal=terminal_id, build=build, server=server, ping_ms=ping)


async def control(
    registry: Registry,
    terminal_id: str,
    action: str,
    *,
    confirm: bool = False,
) -> TerminalStatus:
    """Run ``start``/``stop``/``restart`` on a terminal's systemd unit.

    Mutating a :data:`~mt4ctl.models.Env.LIVE` terminal requires ``confirm=True``.
    """
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"action must be one of {CONTROL_ACTIONS}, got {action!r}")
    term = registry.terminal(terminal_id)
    if term.env is Env.LIVE and not confirm:
        raise ConfirmationRequiredError(terminal_id, action)
    host = registry.host_of(term)
    script = scripts.build_control_script(term.service, action)
    # The script exits with systemctl's code; check=True surfaces a failed
    # start/stop/restart instead of reporting phantom success.
    await ssh.run(host, script, root=True, timeout=45.0, check=True)
    return (await status(registry, [terminal_id]))[0]


async def screenshot(
    registry: Registry, terminal_id: str, *, out_dir: Path | None = None
) -> Path:
    """Capture a terminal window and save the PNG locally, returning its path."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    remote_tmp = f"/tmp/mt4ctl-{uuid.uuid4().hex}.png"
    script = scripts.build_screenshot_script(term.display, term.window_query, remote_tmp)
    result = await ssh.run(host, script, timeout=45.0, check=False)
    if "OK " not in result.stdout:
        raise RemoteCommandError(host.id, 1, "screenshot capture produced no image")
    try:
        data = await ssh.fetch_bytes(host, remote_tmp)
    finally:
        # Always remove the remote image, even if the fetch failed mid-way.
        await ssh.run(host, f"rm -f {scripts.sh_quote(remote_tmp)}", check=False)

    out_dir = out_dir or Path.home() / ".cache" / "mt4ctl"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", term.id)
    local = out_dir / f"{safe_id}.png"
    local.write_bytes(data)
    return local


# --------------------------------------------------------------------------- #
# Deploy
#
# kubectl-apply for one MT4 terminal: reconcile a local bundle onto a terminal,
# idempotently, touching only what mt4ctl manages. Write order is
# stop → DRAIN → backup → apply → start(finally), because MT4 rewrites its `.chr`
# files on exit, so nothing is written until `terminal.exe` is truly gone, and a
# stopped terminal is always brought back. A per-terminal lockdir is held across
# the whole sequence (state read included) to serialize concurrent deploys.
# --------------------------------------------------------------------------- #

# Drain must outlast systemd's TimeoutStopSec (20s on the documented unit) plus
# slack for a slow Wine teardown.
_DRAIN_TIMEOUT_S = 30
# The lock has no heartbeat (one-shot deploy), so the stale-takeover bound must
# exceed the worst-case whole-sequence runtime.
_LOCK_STALE_S = 600


def _parse_remote_state(stdout: str) -> tuple[deploy_core.RemoteState, str]:
    """Parse the remote-state probe into a :class:`RemoteState` + the unit user."""
    unit_user = "root"
    manifest_raw: str | None = None
    remote_hashes: dict[str, str] = {}
    remote_chart_refs: dict[str, str | None] = {}
    dest_stats: dict[str, bool] = {}
    for line in stdout.splitlines():
        parts = line.split(scripts.SEP)
        tag = parts[0]
        if tag == "USER" and len(parts) >= 2:
            unit_user = parts[1].strip() or "root"
        elif tag == "MANIFEST" and len(parts) >= 2:
            token = parts[1].strip()
            manifest_raw = (
                None if token == "MISSING" else base64.b64decode(token).decode("utf-8", "replace")
            )
        elif tag == "CHART" and len(parts) >= 2:
            rel = parts[1]
            name = line.split(scripts.SEP, 2)[2] if len(parts) >= 3 else ""
            remote_chart_refs[rel] = deploy_core.ea_ref_to_ex4(name) if name.strip() else None
        elif tag == "DEST" and len(parts) >= 4:
            rel, exists, sha = parts[1], parts[2], parts[3]
            dest_stats[rel] = exists == "1"
            if exists == "1" and sha != "-":
                remote_hashes[rel] = sha
    state = deploy_core.RemoteState(
        deployed=deploy_core.parse_manifest(manifest_raw),
        remote_hashes=remote_hashes,
        remote_chart_refs=remote_chart_refs,
        dest_stats=dest_stats,
    )
    return state, unit_user


def _build_bundle_tar(bundle_dir: str | Path, rel_paths: list[str]) -> bytes:
    """Pack *rel_paths* from the bundle into an in-memory tar at canonical paths."""
    root = Path(bundle_dir)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in rel_paths:
            deploy_core.validate_member(rel)  # belt-and-suspenders before it ships
            data = (root / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _read_remote_state(
    host: Host, term: Terminal, paths: list[str]
) -> tuple[deploy_core.RemoteState, str]:
    script = scripts.build_remote_state_script(term.data_dir, paths, term.service)
    result = await ssh.run(host, script, timeout=60.0, check=True)
    return _parse_remote_state(result.stdout)


async def _deploy_preflight(host: Host, data_dir: str) -> None:
    result = await ssh.run(host, scripts.build_deploy_preflight_script(data_dir), check=False)
    problems = [
        f"{p[1]}={p[2]}"
        for line in result.stdout.splitlines()
        if (p := line.split(scripts.SEP))[0] == "PRE" and len(p) >= 3 and p[2].split()[0] != "ok"
    ]
    if problems:
        raise DeployError(
            "deploy preflight failed: "
            + "; ".join(problems)
            + " — need GNU tar + a sha256 tool, and staging must share a filesystem "
            "with the chart tree (atomic move)"
        )


async def _drain(host: Host, service: str) -> None:
    result = await ssh.run(
        host,
        scripts.build_drain_script(service, _DRAIN_TIMEOUT_S),
        timeout=_DRAIN_TIMEOUT_S + 15,
        check=False,
    )
    if f"DRAIN{scripts.SEP}ok" not in result.stdout:
        raise DeployError(
            f"terminal process did not exit within {_DRAIN_TIMEOUT_S}s after stop; "
            "aborting before any write (terminal will be restarted)"
        )


def _line_value(stdout: str, tag: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith(tag + scripts.SEP):
            return line.split(scripts.SEP, 1)[1].strip()
    return None


def _parse_cursor(stdout: str) -> tuple[str, int] | None:
    for line in stdout.splitlines():
        if line.startswith("CURSOR" + scripts.SEP):
            parts = line.split(scripts.SEP)
            if len(parts) >= 3:
                try:
                    return parts[1], int(parts[2])
                except ValueError:
                    return parts[1], 0
    return None


_CONN_LABEL = {
    True: "broker connected",
    False: "broker NOT connected",
    None: "broker inconclusive",
}
_LOAD_ERR_WORDS = ("error", "fail", "cannot", "not loaded")


def _ea_log_state(lines: list[str], ea: str) -> str:
    """Classify an expert's load outcome from (lowercased) log lines.

    ``"error"`` if a line mentions it with a load failure, ``"loaded"`` if a line
    mentions it loading cleanly, else ``"absent"``. Best-effort substring match —
    verify is report-only, so a miss never reverts a deploy.
    """
    hits = [ln for ln in lines if ea.lower() in ln]
    if any("load" in ln and any(w in ln for w in _LOAD_ERR_WORDS) for ln in hits):
        return "error"
    if any("load" in ln and not any(w in ln for w in _LOAD_ERR_WORDS) for ln in hits):
        return "loaded"
    return "absent"


async def _verify_deploy(
    registry: Registry,
    term: Terminal,
    host: Host,
    expected_eas: set[str],
    cursor: tuple[str, int] | None,
) -> tuple[bool, str]:
    """Report-only health check: service active + broker + EA-load lines from the log.

    Reads the log *past* a pre-restart cursor (rotation-safe) so a ``loaded`` line
    must be fresh, never inferred from the just-written ``.chr`` (which would be
    circular). ``connected is None`` is inconclusive, not a failure; the live-bit
    is advisory. Never reverts anything.
    """
    st = (await status(registry, [term.id]))[0]
    detail = [f"service={st.service_state}", _CONN_LABEL[st.connected]]

    ea_ok = True
    if expected_eas:
        if cursor is not None:
            script = scripts.build_log_since_script(term.data_dir, cursor[0], cursor[1])
        else:
            script = scripts.build_logs_script(term.data_dir, None, 500)
        try:
            log_text = (await ssh.run(host, script, timeout=30.0, check=False)).stdout
        except RemoteCommandError:
            log_text = ""
        lines = log_text.lower().splitlines()
        missing = [ea for ea in sorted(expected_eas) if _ea_log_state(lines, ea) == "absent"]
        errored = [ea for ea in sorted(expected_eas) if _ea_log_state(lines, ea) == "error"]
        if errored:
            detail.append(f"EA load errors: {', '.join(errored)}")
        if missing:
            detail.append(f"EA load not confirmed: {', '.join(missing)}")
        if not errored and not missing:
            detail.append(f"{len(expected_eas)} experts loaded")
        ea_ok = not errored and not missing

    verify_ok = st.service_state == "active" and st.connected is not False and ea_ok
    return verify_ok, "; ".join(detail)


async def deploy(
    registry: Registry,
    terminal_id: str,
    bundle_dir: str | Path,
    *,
    dry_run: bool = False,
    confirm: bool = False,
) -> DeployResult:
    """Reconcile a local *bundle_dir* onto a terminal (idempotent, managed-subset).

    Pushes the bundle's charts + experts so the terminal's *managed* files match
    it, leaving foreign files (e.g. a watchdog's ``.chr``) untouched. A live
    terminal requires ``confirm=True``. ``dry_run`` previews the plan without a
    lock or any mutation. Recovery from a bad deploy is to re-deploy the previous
    bundle (there is no rollback command); a pre-apply backup is retained and is
    restored internally if an apply fails.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    if term.env is Env.LIVE and not confirm:
        raise ConfirmationRequiredError(terminal_id, "deploy")

    files, chart_to_ex4 = deploy_core.read_bundle(bundle_dir)
    dest_paths = sorted(files)
    expected_eas = {deploy_core.short_name(ex4) for ex4 in chart_to_ex4.values()}

    if dry_run:
        # Read-only preview: no lock (racing only affects the preview, not state).
        state, _user = await _read_remote_state(host, term, dest_paths)
        plan = deploy_core.compute_plan(files, chart_to_ex4, state)
        return DeployResult(
            terminal=term.id,
            plan=plan,
            backup_path=None,
            restarted=False,
            verify_ok=False,
            verify_detail="dry-run (nothing applied)",
            dry_run=True,
        )

    holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = await ssh.run(
        host, scripts.build_lock_acquire_script(term.data_dir, holder, _LOCK_STALE_S), check=False
    )
    if not any(f"LOCK{scripts.SEP}{w}" in lock.stdout for w in ("acquired", "takeover")):
        raise DeployError(
            f"another deploy holds the lock on {terminal_id!r}; retry once it finishes"
        )

    stopped = needs_verify = restarted = verify_ok = False
    backup_path: str | None = None
    apply_error: RemoteCommandError | None = None
    cursor: tuple[str, int] | None = None
    verify_detail = ""
    plan = DeployPlan()
    try:
        state, unit_user = await _read_remote_state(host, term, dest_paths)
        plan = deploy_core.compute_plan(files, chart_to_ex4, state)
        if plan.conflicts:
            raise DeployError(
                "refusing to overwrite files mt4ctl does not manage: "
                + ", ".join(plan.conflicts)
            )
        if not plan.has_changes:
            needs_verify = True  # no changes, but still confirm the terminal is healthy
        else:
            await _deploy_preflight(host, term.data_dir)
            await control(registry, term.id, "stop", confirm=confirm)
            stopped = needs_verify = True
            await _drain(host, term.service)

            # MT4 rewrites `.chr` on exit, so the pre-stop `.chr` classification is
            # stale — recompute it against the quiesced files (`.ex4` is never
            # written by MT4, so its pre-stop diff stands).
            chr_dests = [p for p in dest_paths if p.endswith(".chr")]
            if chr_dests:
                fresh, _ = await _read_remote_state(host, term, chr_dests)
                state = replace(
                    state, remote_hashes={**state.remote_hashes, **fresh.remote_hashes}
                )
                plan = deploy_core.compute_plan(files, chart_to_ex4, state)

            manifest_existing = sorted(state.deployed or {})
            backup = await ssh.run(
                host,
                scripts.build_backup_script(term.data_dir, manifest_existing, _timestamp()),
                check=False,
            )
            backup_val = _line_value(backup.stdout, "BACKUP")
            backup_path = None if backup_val in (None, "none") else backup_val

            place_rel = sorted([*plan.add, *plan.update])
            staging = f"{scripts.STAGING_DIR}/{uuid.uuid4().hex}"
            tar_bytes = _build_bundle_tar(bundle_dir, place_rel)
            await ssh.put_tar(host, tar_bytes, f"{term.data_dir}/{staging}")

            place = {rel: files[rel] for rel in place_rel}
            apply_script = scripts.build_apply_script(
                term.data_dir, staging, place, list(plan.remove), sorted(files), unit_user
            )
            try:
                await ssh.run(host, apply_script, check=True)
            except RemoteCommandError as exc:
                apply_error = exc
                touched = sorted({*plan.add, *plan.update, *plan.remove})
                await ssh.run(
                    host,
                    scripts.build_restore_script(term.data_dir, backup_path, touched, unit_user),
                    check=False,
                )
    finally:
        exc_active = sys.exc_info()[0] is not None
        try:
            if stopped:
                try:
                    cur = await ssh.run(
                        host, scripts.build_log_cursor_script(term.data_dir), check=False
                    )
                    cursor = _parse_cursor(cur.stdout)
                except RemoteCommandError:
                    cursor = None
                try:
                    await control(registry, term.id, "start", confirm=confirm)
                    restarted = True
                except RemoteCommandError:
                    restarted = False
            if needs_verify and not exc_active and apply_error is None:
                verify_ok, verify_detail = await _verify_deploy(
                    registry, term, host, expected_eas, cursor
                )
        finally:
            # The lock is always released, even if start/verify raised above.
            await ssh.run(
                host, scripts.build_lock_release_script(term.data_dir, holder), check=False
            )

    if apply_error is not None:
        raise DeployError(
            f"deploy apply failed ({apply_error}); terminal {terminal_id} was restored to its "
            "pre-deploy state and restarted"
        )
    return DeployResult(
        terminal=term.id,
        plan=plan,
        backup_path=backup_path,
        restarted=restarted,
        verify_ok=verify_ok,
        verify_detail=verify_detail,
        dry_run=False,
    )


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


async def adopt(
    registry: Registry,
    terminal_id: str,
    bundle_dir: str | Path,
    *,
    confirm: bool = False,
) -> AdoptResult:
    """Take an already-running terminal under management (brownfield first cutover).

    Records the bundle's footprint into ``deployed.json`` at the files' current
    on-disk hashes, so a subsequent :func:`deploy` reconciles from that baseline.
    This is records-only: no strategy file is touched, the unit is never stopped or
    restarted. Bundle-scoped — only the bundle's own paths are recorded, so foreign
    files (e.g. a watchdog's chart) stay foreign. Every bundle file must already be
    present on the host (the premise is that the farm runs this bundle); a missing
    file raises :class:`DeployError`. A live terminal requires ``confirm=True``.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    if term.env is Env.LIVE and not confirm:
        raise ConfirmationRequiredError(terminal_id, "adopt")

    files, _chart_to_ex4 = deploy_core.read_bundle(bundle_dir)
    dest_paths = sorted(files)

    holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = await ssh.run(
        host, scripts.build_lock_acquire_script(term.data_dir, holder, _LOCK_STALE_S), check=False
    )
    if not any(f"LOCK{scripts.SEP}{w}" in lock.stdout for w in ("acquired", "takeover")):
        raise DeployError(
            f"another deploy/adopt holds the lock on {terminal_id!r}; retry once it finishes"
        )
    try:
        state, unit_user = await _read_remote_state(host, term, dest_paths)
        # Present-but-unhashable counts as not-adoptable: you cannot record a
        # footprint you could not hash (keeps the manifest from ever holding an
        # empty/forged hash, and the indexing below total).
        absent = [
            rel
            for rel in dest_paths
            if not state.dest_stats.get(rel, False) or not state.remote_hashes.get(rel)
        ]
        if absent:
            raise DeployError(
                "adopt requires every bundle file present and readable on the host (the "
                f"farm is not running this bundle); missing: {', '.join(absent)}"
            )
        manifest_files = {rel: state.remote_hashes[rel] for rel in dest_paths}
        drifted = tuple(rel for rel in dest_paths if state.remote_hashes[rel] != files[rel])
        await ssh.run(
            host,
            scripts.build_manifest_put_script(
                term.data_dir, deploy_core.build_manifest(manifest_files), unit_user
            ),
            check=True,
        )
    finally:
        await ssh.run(
            host, scripts.build_lock_release_script(term.data_dir, holder), check=False
        )

    return AdoptResult(
        terminal=term.id,
        adopted=tuple(dest_paths),
        drifted=drifted,
        unit_user=unit_user,
        manifest_path=f"{term.data_dir}/{scripts.MANIFEST_REL}",
    )
