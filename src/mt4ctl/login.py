"""Headless first-login for a terminal.

MetaTrader stores its password encrypted with a machine-bound key, so a terminal
copied to a new host cannot auto-login until it authenticates once on that host.
This module automates that bootstrap:

1. stop the systemd unit (frees the terminal slot)
2. write a transient startup config (login/password/server), created fresh by
   ``mktemp`` with mode 600, inside the unit's working directory
3. launch the terminal once, in its own process group, reusing the unit's
   ``WorkingDirectory`` / ``WINEPREFIX`` / ``DISPLAY``
4. wait for ``config/accounts.ini`` to become newer than a marker stamped right
   before launch — MetaTrader's signal that authentication succeeded and
   credentials were re-encrypted for this host
5. an ``EXIT``/signal trap *always* kills that process group (siblings share the
   Wine prefix, so a blanket ``wineserver -k`` is wrong) and shreds the config,
   even if an earlier step fails
6. restart the unit, which now auto-logins from the saved file

After this runs once, the terminal reconnects on its own across restarts.
"""

from __future__ import annotations

from . import auth, scripts, ssh
from .errors import ConfirmationRequiredError, Mt4ctlError, RemoteCommandError
from .models import Env, Registry


def build_login_script(
    service: str,
    data_dir: str,
    account: str,
    server: str,
    password: str,
    *,
    wait_seconds: int = 120,
) -> str:
    """Build the self-contained user-side bootstrap script (see module docstring)."""
    # Credentials are written into a quoted heredoc below (no shell expansion),
    # but a newline could still smuggle in a heredoc terminator or corrupt the
    # ini. MT4 login/server/password are single-line tokens, so reject newlines.
    for field, value in (("account", account), ("server", server), ("password", password)):
        if "\n" in value or "\r" in value:
            raise Mt4ctlError(f"{field} must not contain newlines")
    svc = scripts.sh_quote(service)
    ddir = scripts.sh_quote(data_dir)
    return f"""\
set +e
SVC={svc}
DDIR={ddir}
WORKDIR=$(systemctl show -p WorkingDirectory --value "$SVC" 2>/dev/null)
ENV=$(systemctl show -p Environment --value "$SVC" 2>/dev/null)
WP=$(echo "$ENV" | tr ' ' '\\n' | sed -n 's/^WINEPREFIX=//p' | head -1)
DISP=$(echo "$ENV" | tr ' ' '\\n' | sed -n 's/^DISPLAY=//p' | head -1)
[ -z "$WORKDIR" ] && WORKDIR="$DDIR"
[ -z "$DISP" ] && DISP=:0

INI=""
MARKER=""
PGID=""
cleanup() {{
  [ -n "$PGID" ] && kill -TERM -"$PGID" 2>/dev/null
  sleep 1
  [ -n "$PGID" ] && kill -KILL -"$PGID" 2>/dev/null
  [ -n "$INI" ] && {{ shred -u "$INI" 2>/dev/null || rm -f "$INI"; }}
  [ -n "$MARKER" ] && rm -f "$MARKER" 2>/dev/null
}}
trap cleanup EXIT HUP INT TERM

INI=$(mktemp "$WORKDIR/mt4ctl-login.XXXXXX.ini" 2>/dev/null) || {{ echo "LOGIN|error=mktemp"; exit 1; }}
chmod 600 "$INI"
INIBASE=$(basename "$INI")
ACCFILE="$DDIR/config/accounts.ini"
MARKER=$(mktemp 2>/dev/null) || MARKER="/tmp/mt4ctl-marker.$$"

cat > "$INI" <<'INICFG'
[Common]
Login={account}
Password={password}
Server={server}
KeepPrivate=1
NewsEnable=false
CertInstall=false
[Experts]
AllowLiveTrading=false
Enabled=false
INICFG

cd "$WORKDIR" || {{ echo "LOGIN|error=workdir"; exit 1; }}
: > "$MARKER"
sleep 1
WINEPREFIX="$WP" DISPLAY="$DISP" setsid wine terminal.exe /portable "$INIBASE" >/dev/null 2>&1 &
PGID=$!

ok=0
for _ in $(seq 1 {wait_seconds}); do
  if [ "$ACCFILE" -nt "$MARKER" ]; then ok=1; break; fi
  sleep 1
done
echo "LOGIN|ok=$ok"
"""


async def login(
    registry: Registry,
    terminal_id: str,
    *,
    account: str | None = None,
    server: str,
    password: str | None = None,
    confirm: bool = False,
    wait_seconds: int = 120,
) -> str:
    """Perform a one-time headless login, then restart the unit for auto-reconnect.

    Args:
        terminal_id: terminal to log in.
        account: login number; defaults to the terminal's configured account.
        server: broker server name, e.g. ``ExampleBroker-Demo``.
        password: explicit password; otherwise resolved via :mod:`mt4ctl.auth`.
        confirm: required (``True``) for live terminals.
        wait_seconds: how long to wait for authentication to land.

    Returns:
        A short human-readable summary of the outcome.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    if term.env is Env.LIVE and not confirm:
        raise ConfirmationRequiredError(terminal_id, "login")

    login_account = account or term.account
    if not login_account:
        raise Mt4ctlError(f"terminal {terminal_id!r} has no account configured; pass account=")
    secret = auth.resolve_password(login_account, password)

    # Build and validate the bootstrap *before* stopping the unit, so a bad
    # value (e.g. a newline) can never leave the terminal stopped.
    script = build_login_script(
        term.service, term.data_dir, login_account, server, secret, wait_seconds=wait_seconds
    )

    # Stop the unit so the one-shot owns the terminal slot; abort if it fails
    # (the unit is untouched, so there is nothing to restore).
    await ssh.run(
        host, scripts.build_control_script(term.service, "stop"), root=True, check=True
    )

    bootstrap_out = ""
    bootstrap_error: RemoteCommandError | None = None
    try:
        result = await ssh.run(host, script, timeout=wait_seconds + 30, check=False)
        bootstrap_out = result.stdout
    except RemoteCommandError as exc:
        bootstrap_error = exc
    finally:
        # Always bring the unit back, even if the bootstrap timed out or raised.
        try:
            await ssh.run(
                host,
                scripts.build_control_script(term.service, "start"),
                root=True,
                check=True,
            )
            restarted = True
        except RemoteCommandError:
            restarted = False

    if bootstrap_error is not None:
        msg = (
            f"{terminal_id}: login bootstrap failed ({bootstrap_error}); "
            f"account {login_account} on {server} was not logged in."
        )
    elif "LOGIN|ok=1" in bootstrap_out:
        msg = (
            f"{terminal_id}: logged in to account {login_account} on {server}; "
            f"credentials saved."
        )
    else:
        msg = (
            f"{terminal_id}: login did NOT confirm within {wait_seconds}s "
            f"(account {login_account} on {server}); check `mt4_logs` and verify the "
            f"server name and password."
        )
    # The restart outcome is reported on every path so a stopped unit is never
    # hidden behind a login message.
    if restarted:
        return msg + " Unit restarted for auto-reconnect."
    return msg + " WARNING: restarting the unit failed — run `mt4_control` start."
