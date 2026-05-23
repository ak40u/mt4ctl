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

from collections.abc import Iterable

CONTROL_ACTIONS = ("start", "stop", "restart")

# Field separator for the status line protocol. Chosen so it never collides with
# log text (we strip it from captured log lines before emitting).
SEP = "|"


def sh_quote(value: str) -> str:
    """POSIX single-quote a string for safe embedding in a bash literal."""
    return "'" + value.replace("'", "'\\''") + "'"


def build_status_script(broker_host: str | None, specs: Iterable[tuple[str, str, str]]) -> str:
    """Build a host script that reports status for several terminals at once.

    Each spec is ``(terminal_id, systemd_unit, data_dir)``. For every terminal
    it emits one line:

        ``TERM|<id>|<service_state>|<log_age_s|-1>|<estab443|-1>|<last_event>``

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
  echo "TERM{SEP}$id{SEP}$state{SEP}$age{SEP}$estab{SEP}$last"
}}

{calls}
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
