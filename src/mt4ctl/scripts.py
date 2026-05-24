"""Builders for the remote bash snippets run by :mod:`mt4ctl.operations`.

Every function here is pure: it returns a bash string and touches nothing. That
keeps the shell logic unit-testable and the orchestration layer free of quoting
concerns. Output uses a ``|``-delimited line protocol parsed back in
:mod:`mt4ctl.operations`.

Invariant: any registry- or caller-supplied value embedded in a script is either
``sh_quote``-d or written inside a single-quoted heredoc. Nothing is
interpolated raw into a double-quoted shell context.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping

CONTROL_ACTIONS = ("start", "stop", "restart")

# Field separator for the status line protocol. Chosen so it never collides with
# log text (we strip it from captured log lines before emitting).
SEP = "|"

# mt4ctl's private namespace inside a terminal's data dir. Everything the deploy
# pipeline writes (staging, backups, manifest, lockdir) lives here, well away from
# MT4's own tree, so it is trivially distinguishable from managed/foreign files.
MT4CTL_DIR = ".mt4ctl"
MANIFEST_REL = f"{MT4CTL_DIR}/deployed.json"
BACKUP_DIR = f"{MT4CTL_DIR}/backups"
STAGING_DIR = f"{MT4CTL_DIR}/staging"
# Canonical lockdir name — referenced identically by acquire/release. An atomic
# mkdir is the lock primitive (it survives across separate SSH invocations, which
# a per-process flock cannot).
LOCKDIR = f"{MT4CTL_DIR}/deploy.lock.d"

# A portable sha256-of-a-file shell function, defined once and reused by the
# scripts that hash remote files (state read + apply verification).
_SHA_FUNC = (
    "sha() { "
    "if command -v sha256sum >/dev/null 2>&1; then sha256sum \"$1\" | awk '{print $1}'; "
    "elif command -v shasum >/dev/null 2>&1; then shasum -a 256 \"$1\" | awk '{print $1}'; "
    "else openssl dgst -sha256 \"$1\" | awk '{print $NF}'; fi; }"
)


def sh_quote(value: str) -> str:
    """POSIX single-quote a string for safe embedding in a bash literal."""
    return "'" + value.replace("'", "'\\''") + "'"


def build_status_script(broker_host: str | None, specs: Iterable[tuple[str, str, str]]) -> str:
    """Build a host script that reports status for several terminals at once.

    Each spec is ``(terminal_id, systemd_unit, data_dir)``. For every terminal
    it emits one line:

        ``TERM|<id>|<service_state>|<log_age_s|-1>|<estab443|-1>|<last_event>|<active_s|-1>``

    The trailing ``active_s`` is the unit's uptime (seconds since it became active,
    from ``ActiveEnterTimestampMonotonic`` vs ``/proc/uptime``), or ``-1`` when
    unknown — it lets a just-restarted *connecting* terminal be told apart from one
    that is persistently down.

    Connection is attributed per terminal by intersecting the service cgroup's
    ``terminal.exe`` PIDs with established ``:443`` sockets (optionally filtered
    to the broker's resolved IPs). ``estab=-1`` means *unknown* and is emitted
    rather than a guess when attribution is not reliable: broker resolution
    failed, ``ss`` exposed no process metadata (e.g. non-root, foreign user), or
    the terminal owns no ``terminal.exe`` process.
    """
    broker = sh_quote(broker_host) if broker_host else "''"
    calls = "\n".join(
        f"emit {sh_quote(tid)} {sh_quote(svc)} {sh_quote(data_dir)}"
        for tid, svc, data_dir in specs
    )
    return f"""\
set +e
NOW=$(date +%s)
BROKER={broker}
BIPS=""
BROKER_FAIL=0
if [ -n "$BROKER" ]; then
  BIPS=$(getent ahosts "$BROKER" 2>/dev/null | awk '{{print $1}}' | sort -u | tr '\\n' ' ')
  [ -z "$BIPS" ] && BROKER_FAIL=1
fi
SS=$(ss -tnp 2>/dev/null)
ESTABS=$(printf '%s\\n' "$SS" | awk '/ESTAB/ && $5 ~ /:443$/ {{print}}')
CURUID=$(id -u 2>/dev/null)
CURUSER=$(id -un 2>/dev/null)

emit() {{
  id="$1"; svc="$2"; dir="$3"
  state=$(systemctl is-active "$svc" 2>/dev/null)
  [ -n "$state" ] || state=unknown
  log=$(ls -t "$dir"/logs/*.log 2>/dev/null | head -1)
  if [ -n "$log" ]; then
    mtime=$(stat -c %Y "$log" 2>/dev/null || echo "$NOW")
    age=$((NOW - mtime))
    last=$(grep -aiE 'login on|no connection|disconnect|authoriz' "$log" 2>/dev/null \\
           | tail -1 | tr '{SEP}' '/' | sed 's/^[0-9 :.\\t]*//')
  else
    age=-1; last=""
  fi
  cg=$(systemctl show -p ControlGroup --value "$svc" 2>/dev/null)
  pids=""
  for p in $(cat "/sys/fs/cgroup${{cg}}/cgroup.procs" 2>/dev/null); do
    case "$(cat /proc/$p/comm 2>/dev/null)" in
      terminal.exe|terminal64.exe) pids="$pids $p" ;;
    esac
  done
  # Socket ownership is visible only to root or the unit's own user; otherwise
  # report unknown (-1) rather than a false 'down'.
  svcuser=$(systemctl show -p User --value "$svc" 2>/dev/null)
  attrib=0
  if [ "$CURUID" = 0 ]; then attrib=1
  elif [ -n "$svcuser" ] && [ "$svcuser" = "$CURUSER" ]; then attrib=1
  fi
  estab=-1
  if [ "$BROKER_FAIL" = 0 ] && [ "$attrib" = 1 ] && [ -n "$pids" ]; then
    estab=0
    while IFS= read -r ln; do
      [ -z "$ln" ] && continue
      peer=$(echo "$ln" | awk '{{print $5}}')
      ip=${{peer%:*}}; ip=${{ip#[}}; ip=${{ip%]}}
      if [ -n "$BIPS" ]; then case " $BIPS " in *" $ip "*) : ;; *) continue ;; esac; fi
      for p in $pids; do case "$ln" in *"pid=$p,"*) estab=$((estab+1)); break ;; esac; done
    done <<< "$ESTABS"
  fi
  # Unit uptime: monotonic active-since vs current boot clock, so a fresh restart
  # reads as a small value regardless of wall-clock skew. -1 when unresolved.
  aem=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$svc" 2>/dev/null)
  active=-1
  if [ -n "$aem" ] && [ "$aem" -gt 0 ] 2>/dev/null; then
    up=$(awk '{{print $1}}' /proc/uptime 2>/dev/null)
    [ -n "$up" ] && active=$(awk -v u="$up" -v a="$aem" 'BEGIN{{d=u-a/1000000; if(d<0)d=0; printf "%d", d}}')
  fi
  echo "TERM{SEP}$id{SEP}$state{SEP}$age{SEP}$estab{SEP}$last{SEP}$active"
}}

{calls}
"""


def build_experts_script(data_dir: str) -> str:
    """Build a script reporting the AutoTrading master switch + attached experts.

    Emits ``MASTER|<0|1|?>`` (from ``config/terminal.ini`` ``Experts=``) and one
    ``EA|<name>|<flags>`` line per attached expert (parsed from the active
    profile's ``.chr`` files). ``flags`` is the MT4 chart-expert bitmask.
    """
    dir_q = sh_quote(data_dir)
    return f"""\
set +e
DIR={dir_q}
m=$(grep -iE '^Experts=' "$DIR/config/terminal.ini" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '\\r')
[ -z "$m" ] && m="?"
echo "MASTER{SEP}$m"
for f in "$DIR"/profiles/default/*.chr; do
  [ -e "$f" ] || continue
  # .chr files are CRLF — strip the trailing CR so it doesn't ride into the name
  # field, where it would become a stray splitlines() boundary downstream (each
  # EA record would split apart) and dirty the parsed name.
  awk '{{sub(/\\r$/,"")}} /<expert>/{{e=1;n=""}} e&&/^name=/{{n=substr($0,6)}} e&&/^flags=/{{print "EA{SEP}"n"{SEP}"substr($0,7); e=0}}' "$f"
done
"""


def build_info_script(data_dir: str) -> str:
    """Build a script reporting build, broker, and last login+ping from the log."""
    dir_q = sh_quote(data_dir)
    return f"""\
set +e
DIR={dir_q}
if ! ls "$DIR"/logs/*.log >/dev/null 2>&1; then echo "INFO{SEP}nolog"; exit 0; fi
# Scan all logs (the last login/ping may be in an earlier file than the newest).
echo "BUILD{SEP}$(grep -ahoE '([A-Za-z0-9]+ MT[45] )?build [0-9]+' "$DIR"/logs/*.log | tail -1)"
echo "LOGIN{SEP}$(grep -ahoE 'login on [^ ]+ through[^(]*\\(ping: [0-9.]+ ms\\)' "$DIR"/logs/*.log | tail -1)"
"""


def build_logs_script(data_dir: str, pattern: str | None, lines: int) -> str:
    """Build a script that returns the tail of a terminal's newest log file."""
    grep = (
        f'grep -aiE {sh_quote(pattern)} "$log" 2>/dev/null | tail -n {lines}'
        if pattern
        else f'tail -n {lines} "$log"'
    )
    return f"""\
set +e
DIR={sh_quote(data_dir)}
log=$(ls -t "$DIR"/logs/*.log 2>/dev/null | head -1)
if [ -z "$log" ]; then printf '(no log files in %s/logs)\\n' "$DIR"; exit 0; fi
echo "# $log"
{grep}
"""


def build_control_script(service: str, action: str) -> str:
    """Build a script that runs a systemd action and reports the resulting state.

    The script exits with systemctl's return code so the caller can treat a
    failed start/stop/restart as an error rather than silent success.
    """
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"action must be one of {CONTROL_ACTIONS}, got {action!r}")
    svc = sh_quote(service)
    return f"""\
set +e
systemctl {action} {svc}
rc=$?
sleep 1
st=$(systemctl is-active {svc} 2>/dev/null)
[ -n "$st" ] || st=unknown
echo "STATE{SEP}$st{SEP}rc=$rc"
exit $rc
"""


def build_doctor_script(specs: Iterable[tuple[str, str, str]]) -> str:
    """Build a host probe for ``doctor``: required tools and per-terminal sanity.

    Emits ``TOOL|<name>|ok|missing`` (core + screenshot tools) and, per terminal,
    ``TERM|<id>|<found|notfound>|<ok|missing>`` for the systemd unit and data dir.
    """
    calls = "\n".join(
        f"checkterm {sh_quote(tid)} {sh_quote(svc)} {sh_quote(data_dir)}"
        for tid, svc, data_dir in specs
    )
    return f"""\
set +e
for t in systemctl ss getent stat base64; do
  command -v "$t" >/dev/null 2>&1 && echo "TOOL{SEP}$t{SEP}ok" || echo "TOOL{SEP}$t{SEP}missing"
done
for t in import scrot xdotool; do
  command -v "$t" >/dev/null 2>&1 && echo "XTOOL{SEP}$t{SEP}ok" || echo "XTOOL{SEP}$t{SEP}missing"
done
checkterm() {{
  id="$1"; svc="$2"; dir="$3"
  if systemctl cat "$svc" >/dev/null 2>&1; then unit=found; else unit=notfound; fi
  if [ -d "$dir" ]; then dd=ok; else dd=missing; fi
  echo "TERM{SEP}$id{SEP}$unit{SEP}$dd"
}}
{calls}
"""


def build_screenshot_script(display: str, window_query: str | None, out_path: str) -> str:
    """Build a script that captures a terminal window to *out_path* as PNG.

    Shared-display hosts stack several windows, so we raise and focus the target
    window (matched by account number) before grabbing the root image.
    """
    out = sh_quote(out_path)
    activate = ""
    if window_query:
        q = sh_quote(window_query)
        activate = f"""\
WID=$(xdotool search --name {q} 2>/dev/null | head -1)
if [ -n "$WID" ]; then
  xdotool windowactivate --sync "$WID" 2>/dev/null
  xdotool windowraise "$WID" 2>/dev/null
  sleep 1
fi
"""
    return f"""\
set +e
umask 077
export DISPLAY={sh_quote(display)}
{activate}import -window root {out} 2>/dev/null || scrot -o {out} 2>/dev/null
test -s {out} && echo "OK {out}" || echo "FAIL"
"""


# --------------------------------------------------------------------------- #
# Deploy pipeline builders
#
# These produce the remote side of `operations.deploy`. The orchestrator holds a
# single lock across the whole sequence and runs file mutations as the unit's
# own user (the SSH user is the unit `User=` on this farm) so MetaTrader can
# still rewrite `.chr`/`deployed.json` on exit; only `systemctl` runs as root.
# --------------------------------------------------------------------------- #


def build_remote_state_script(data_dir: str, dest_paths: Iterable[str], unit: str) -> str:
    """Report everything the diff needs in one read: manifest, per-destination
    existence + hash, the live chart set with EA refs, and the unit's ``User=``.

    Emits ``SEP``-framed lines:

    * ``USER|<unit User= or root>``
    * ``MANIFEST|<base64 of deployed.json | MISSING>``
    * ``CHART|<relpath>|<raw name= value>`` per live ``profiles/default/*.chr``
    * ``DEST|<relpath>|<0|1>|<sha256|->`` per bundle destination

    Hashes only the bundle destinations (not the whole Experts tree): add/update/
    unchanged are decided against these, while removals are decided from the
    manifest + chart refs, so no broader hashing is needed.
    """
    dir_q = sh_quote(data_dir)
    unit_q = sh_quote(unit)
    dest_calls = "\n".join(f"emit_dest {sh_quote(p)}" for p in dest_paths)
    return f"""\
set +e
{_SHA_FUNC}
DIR={dir_q}
UNIT={unit_q}
U=$(systemctl show -p User --value "$UNIT" 2>/dev/null)
[ -z "$U" ] && U=root
echo "USER{SEP}$U"
MF="$DIR/{MANIFEST_REL}"
if [ -f "$MF" ]; then
  echo "MANIFEST{SEP}$(base64 -w0 "$MF" 2>/dev/null || base64 "$MF" | tr -d '\\n')"
else
  echo "MANIFEST{SEP}MISSING"
fi
for f in "$DIR"/profiles/default/*.chr; do
  [ -e "$f" ] || continue
  rel="profiles/default/$(basename "$f")"
  name=$(awk '{{sub(/\\r$/,"")}} /<expert>/{{e=1}} e&&/^name=/{{print substr($0,6); exit}}' "$f")
  echo "CHART{SEP}$rel{SEP}$name"
done
emit_dest() {{
  rel="$1"; p="$DIR/$rel"
  if [ -f "$p" ]; then echo "DEST{SEP}$rel{SEP}1{SEP}$(sha "$p")"; else echo "DEST{SEP}$rel{SEP}0{SEP}-"; fi
}}
{dest_calls}
"""


def build_drain_script(unit: str, timeout_s: int) -> str:
    """Wait (up to *timeout_s*) for the unit's ``terminal.exe`` to leave its cgroup.

    MT4 rewrites ``profiles/default/*.chr`` on exit, so apply must not touch the
    tree until the process is truly gone. Exits non-zero if it persists, so the
    orchestrator aborts apply rather than writing under a live terminal.
    """
    unit_q = sh_quote(unit)
    return f"""\
set +e
UNIT={unit_q}
cg=$(systemctl show -p ControlGroup --value "$UNIT" 2>/dev/null)
for _ in $(seq 1 {int(timeout_s)}); do
  found=0
  for p in $(cat "/sys/fs/cgroup${{cg}}/cgroup.procs" 2>/dev/null); do
    case "$(cat /proc/$p/comm 2>/dev/null)" in
      terminal.exe|terminal64.exe) found=1; break ;;
    esac
  done
  if [ "$found" = 0 ]; then echo "DRAIN{SEP}ok"; exit 0; fi
  sleep 1
done
echo "DRAIN{SEP}timeout"
exit 1
"""


def build_backup_script(data_dir: str, managed_paths: Iterable[str], ts: str) -> str:
    """Tar the currently-present managed files **plus the manifest** into
    ``<data_dir>/.mt4ctl/backups/<ts>.tar`` and prune to the newest 3.

    A no-op (``BACKUP|none``) when nothing managed is present (first deploy).
    """
    dir_q = sh_quote(data_dir)
    ts_q = sh_quote(ts)
    adds = "\n".join(
        f'[ -f "$DIR/"{sh_quote(p)} ] && printf \'%s\\n\' {sh_quote(p)} >> "$LIST"'
        for p in managed_paths
    )
    return f"""\
set -e
DIR={dir_q}
TS={ts_q}
BK="$DIR/{BACKUP_DIR}"
mkdir -p "$BK"
LIST=$(mktemp)
{adds}
[ -f "$DIR/{MANIFEST_REL}" ] && printf '%s\\n' {sh_quote(MANIFEST_REL)} >> "$LIST"
if [ -s "$LIST" ]; then
  tar -C "$DIR" -cf "$BK/$TS.tar" -T "$LIST"
  echo "BACKUP{SEP}$BK/$TS.tar"
else
  echo "BACKUP{SEP}none"
fi
rm -f "$LIST"
ls -1t "$BK"/*.tar 2>/dev/null | tail -n +4 | while IFS= read -r old; do rm -f "$old"; done
"""


def _json_str(value: str) -> str:
    """A bash single-quoted literal that *is* a JSON string (for manifest keys)."""
    return sh_quote(json.dumps(value))


def build_apply_script(
    data_dir: str,
    staging_dir: str,
    place: Mapping[str, str],
    remove_paths: Iterable[str],
    manifest_paths: Iterable[str],
    unit_user: str,
) -> str:
    """Verify staged files, place them atomically, remove dropped ones, then write
    ``deployed.json`` LAST from the **on-disk** hashes — all owned by *unit_user*.

    *place* maps each final relpath (also its path inside *staging_dir*) to its
    expected sha256: every staged file is hashed and compared **before any move**,
    so a truncated/corrupt upload aborts the apply before it mutates the tree.
    *manifest_paths* is the full desired managed set (add+update+unchanged); the
    manifest is rebuilt from what is actually on disk so it can never claim a file
    that was not faithfully written. ``set -e`` aborts on the first failure.
    """
    dir_q = sh_quote(data_dir)
    stg_q = sh_quote(staging_dir)
    user_q = sh_quote(unit_user)
    verify = "\n".join(
        f'f={sh_quote(rel)}; got=$(sha "$STG/$f"); '
        f'[ "$got" = {sh_quote(sha)} ] || {{ echo "APPLY{SEP}hashfail{SEP}$f"; exit 1; }}'
        for rel, sha in place.items()
    )
    moves = "\n".join(
        f'mkdir -p "$(dirname "$DIR/"{sh_quote(rel)})"; mv -f "$STG/"{sh_quote(rel)} "$DIR/"{sh_quote(rel)}'
        for rel in place
    )
    removes = "\n".join(f'rm -f "$DIR/"{sh_quote(rel)}' for rel in remove_paths)
    manifest_entries = "\n".join(
        f'h=$(sha "$DIR/"{sh_quote(rel)}); '
        f'[ "$FIRST" = 1 ] && FIRST=0 || printf \',\' >> "$TMP"; '
        f'printf \'%s:"%s"\' {_json_str(rel)} "$h" >> "$TMP"'
        for rel in manifest_paths
    )
    # chown every write + the private dir to the unit user so MT4 (running as that
    # user) can persist `.chr`/`deployed.json` on exit. Best-effort: a no-op when
    # apply already runs as that user, and skipped entirely for a root unit.
    chown_targets = " ".join(sh_quote(p) for p in [*place.keys(), MANIFEST_REL, MT4CTL_DIR])
    return f"""\
set -e
{_SHA_FUNC}
DIR={dir_q}
STG="$DIR/"{stg_q}
U={user_q}
{verify}
{moves}
{removes}
TMP=$(mktemp "$DIR/{MT4CTL_DIR}/dep.XXXXXX")
FIRST=1
printf '{{"version":1,"files":{{' > "$TMP"
{manifest_entries}
printf '}}}}' >> "$TMP"
mv -f "$TMP" "$DIR/{MANIFEST_REL}"
if [ "$U" != root ]; then
  for t in {chown_targets}; do chown -R "$U" "$DIR/$t" 2>/dev/null || true; done
fi
echo "APPLY{SEP}ok"
"""


def build_manifest_put_script(data_dir: str, manifest_json: str, unit_user: str) -> str:
    """Write a ready-made ``deployed.json`` payload atomically, owned by *unit_user*.

    Used by ``adopt`` to record an already-running bundle as managed without
    touching any strategy file. The JSON is shipped base64-encoded and decoded
    remotely (binary-safe, zero quoting risk), written via temp + ``mv``, then
    ``chown``ed to the unit user — skipped when that user is ``root`` (same guard
    as apply: when ``User=`` can't be resolved it stays owned by the SSH user,
    which is the unit user on a correctly-configured host).
    """
    dir_q = sh_quote(data_dir)
    user_q = sh_quote(unit_user)
    payload = base64.b64encode(manifest_json.encode("utf-8")).decode("ascii")
    return f"""\
set -e
DIR={dir_q}
U={user_q}
mkdir -p "$DIR/{MT4CTL_DIR}"
TMP=$(mktemp "$DIR/{MT4CTL_DIR}/dep.XXXXXX")
echo {payload} | base64 -d > "$TMP"
mv -f "$TMP" "$DIR/{MANIFEST_REL}"
if [ "$U" != root ]; then chown "$U" "$DIR/{MANIFEST_REL}" "$DIR/{MT4CTL_DIR}" 2>/dev/null || true; fi
echo "ADOPT{SEP}ok"
"""


def build_lock_acquire_script(data_dir: str, holder_id: str, stale_s: int) -> str:
    """Acquire the per-terminal deploy lock via an atomic ``mkdir`` of the lockdir.

    Records *holder_id* + timestamp; fails (exit 1, ``LOCK|held``) when another
    holder owns it, unless its timestamp is older than *stale_s* (a single-shot
    deploy has no heartbeat, so *stale_s* must exceed the worst-case run).
    """
    dir_q = sh_quote(data_dir)
    holder_q = sh_quote(holder_id)
    return f"""\
set +e
DIR={dir_q}
H={holder_q}
LD="$DIR/{LOCKDIR}"
mkdir -p "$DIR/{MT4CTL_DIR}"
if mkdir "$LD" 2>/dev/null; then
  printf '%s %s\\n' "$H" "$(date +%s)" > "$LD/owner"
  echo "LOCK{SEP}acquired"; exit 0
fi
ts=$(awk '{{print $2}}' "$LD/owner" 2>/dev/null)
now=$(date +%s)
if [ -n "$ts" ] && [ $((now - ts)) -gt {int(stale_s)} ]; then
  rm -rf "$LD"
  if mkdir "$LD" 2>/dev/null; then
    printf '%s %s\\n' "$H" "$now" > "$LD/owner"
    echo "LOCK{SEP}takeover"; exit 0
  fi
fi
echo "LOCK{SEP}held"; exit 1
"""


def build_lock_release_script(data_dir: str, holder_id: str) -> str:
    """Release the deploy lock, but only if this *holder_id* still owns it."""
    dir_q = sh_quote(data_dir)
    holder_q = sh_quote(holder_id)
    return f"""\
set +e
DIR={dir_q}
H={holder_q}
LD="$DIR/{LOCKDIR}"
owner=$(awk '{{print $1}}' "$LD/owner" 2>/dev/null)
if [ "$owner" = "$H" ]; then rm -rf "$LD"; echo "LOCK{SEP}released"; else echo "LOCK{SEP}notowner"; fi
"""


def build_restore_script(
    data_dir: str,
    backup_tar: str | None,
    touched_paths: Iterable[str],
    unit_user: str,
) -> str:
    """Internal restore on apply failure: purge *touched_paths* first (so a
    brand-new file absent from the backup is removed), then — if *backup_tar* is
    given — extract the pre-apply backup over the tree, owned by *unit_user*.

    *backup_tar* is ``None`` on a first deploy (the backup was a no-op); the purge
    alone then returns the terminal to its clean pre-deploy state. Best-effort
    throughout (``set +e``): restore must attempt every step before ``start``.
    """
    dir_q = sh_quote(data_dir)
    user_q = sh_quote(unit_user)
    purges = "\n".join(f'rm -f "$DIR/"{sh_quote(p)}' for p in touched_paths)
    extract = ""
    if backup_tar is not None:
        extract = f"""\
tar -C "$DIR" -xf {sh_quote(backup_tar)} --no-same-owner 2>/dev/null
if [ "$U" != root ]; then chown -R "$U" "$DIR/{MT4CTL_DIR}" 2>/dev/null || true; fi
"""
    return f"""\
set +e
DIR={dir_q}
U={user_q}
{purges}
{extract}echo "RESTORE{SEP}done"
"""


def build_reset_market_watch_script(data_dir: str, ts: str) -> str:
    """Back up then delete each ``history/*/symbols.sel`` so MT4 rebuilds Market Watch.

    Run **only** inside the deploy's stopped window (after drain): a live terminal
    rewrites the file from its in-memory Market Watch on exit, so the delete must
    follow the process leaving. On the next start MT4 rebuilds Market Watch as the
    broker default set plus every loaded chart's symbol — capping unbounded
    carry-over without any binary write (``symbols.sel`` is an undocumented binary
    we never author). Every removed file is copied to the backups dir first, and
    those copies are pruned to the newest few so folding the reset into every
    deploy cannot grow the backups dir without bound. Best-effort (``set +e``): a
    missing file is a no-op. Emits ``MWRESET|<count>``.
    """
    dir_q = sh_quote(data_dir)
    ts_q = sh_quote(ts)
    return f"""\
set +e
DIR={dir_q}
TS={ts_q}
BK="$DIR/{BACKUP_DIR}"
mkdir -p "$BK"
n=0
for f in "$DIR"/history/*/symbols.sel; do
  [ -e "$f" ] || continue
  srv=$(basename "$(dirname "$f")")
  # Delete only if the backup copy succeeded — never drop the live file without one.
  cp -f "$f" "$BK/symbols.sel.$srv.$TS.bak" 2>/dev/null && rm -f "$f" && n=$((n+1))
done
ls -1t "$BK"/symbols.sel.*.bak 2>/dev/null | tail -n +7 | while IFS= read -r old; do rm -f "$old"; done
echo "MWRESET{SEP}$n"
"""


def build_log_cursor_script(data_dir: str) -> str:
    """Capture a pre-restart log cursor: ``CURSOR|<newest log path|>|<size>``.

    Recorded *before* ``control start`` so verify can read only the lines a
    restart produces, never a stale ``loaded`` from before the deploy.
    """
    dir_q = sh_quote(data_dir)
    return f"""\
set +e
DIR={dir_q}
log=$(ls -t "$DIR"/logs/*.log 2>/dev/null | head -1)
if [ -z "$log" ]; then echo "CURSOR{SEP}{SEP}0"; else echo "CURSOR{SEP}$log{SEP}$(stat -c %s "$log" 2>/dev/null || echo 0)"; fi
"""


def build_log_since_script(data_dir: str, cursor_path: str, cursor_size: int) -> str:
    """Emit the terminal log written since a cursor (rotation- & truncation-safe).

    Reads the cursor file beyond *cursor_size* (or whole, if it shrank — a rotated/
    truncated file), plus any log whose name sorts after the cursor's (MT4 rotates
    to a new ``YYYYMMDD.log`` on restart, so lexical order is chronological). An
    empty *cursor_path* (no log at capture time) reads every current log.
    """
    dir_q = sh_quote(data_dir)
    cp_q = sh_quote(cursor_path)
    return f"""\
set +e
DIR={dir_q}
CP={cp_q}
CS={int(cursor_size)}
if [ -n "$CP" ] && [ -f "$CP" ]; then
  sz=$(stat -c %s "$CP" 2>/dev/null || echo 0)
  if [ "$sz" -ge "$CS" ]; then tail -c +$((CS + 1)) "$CP"; else cat "$CP"; fi
fi
cb=$(basename "$CP" 2>/dev/null)
for f in "$DIR"/logs/*.log; do
  [ -e "$f" ] || continue
  bn=$(basename "$f")
  if [ -z "$cb" ] || [ "$bn" \\> "$cb" ]; then cat "$f"; fi
done
"""


def build_deploy_preflight_script(data_dir: str) -> str:
    """Deploy-only host checks: GNU ``tar`` + a sha256 tool, and that the staging
    namespace and the chart tree share one filesystem (atomic ``mv`` needs it).

    Emits ``PRE|tar|<ok|missing>``, ``PRE|sha|<ok|missing>``, and
    ``PRE|device|<ok|mismatch …>``. The orchestrator fails the deploy on any
    non-ok line; doctor stays green for lifecycle-only users (these are not core
    tools).
    """
    dir_q = sh_quote(data_dir)
    return f"""\
set +e
DIR={dir_q}
mkdir -p "$DIR/{MT4CTL_DIR}"
if tar --version 2>/dev/null | grep -qi gnu; then echo "PRE{SEP}tar{SEP}ok"; else echo "PRE{SEP}tar{SEP}missing"; fi
if command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || command -v openssl >/dev/null 2>&1; then
  echo "PRE{SEP}sha{SEP}ok"
else
  echo "PRE{SEP}sha{SEP}missing"
fi
d1=$(stat -c %d "$DIR/{MT4CTL_DIR}" 2>/dev/null)
ok=1; bad=""
# Both managed trees receive atomic `mv`s from staging, so both must share the
# staging device — not just the chart tree.
for sub in profiles/default MQL4/Experts; do
  ref="$DIR/$sub"; [ -d "$ref" ] || ref="$DIR"
  d2=$(stat -c %d "$ref" 2>/dev/null)
  if [ -z "$d1" ] || [ -z "$d2" ] || [ "$d1" != "$d2" ]; then ok=0; bad="$bad $sub=$d2"; fi
done
if [ "$ok" = 1 ]; then echo "PRE{SEP}device{SEP}ok"; else echo "PRE{SEP}device{SEP}mismatch staging=$d1 vs$bad"; fi
"""
