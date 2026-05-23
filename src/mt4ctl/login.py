"""Headless first-login for a terminal.

MetaTrader stores its password encrypted with a machine-bound key, so a terminal
copied to a new host cannot auto-login until it authenticates once on that host.
This module automates that bootstrap:

1. stop the systemd unit (frees the terminal slot)
2. write a transient startup config (login/password/server) with mode 600
3. launch the terminal once, in its own process group, reusing the unit's
   ``WorkingDirectory`` / ``WINEPREFIX`` / ``DISPLAY``
4. wait for ``config/accounts.ini`` to be rewritten — MetaTrader's signal that
   authentication succeeded and credentials were re-encrypted for this host
5. kill *only* that process group (siblings share the Wine prefix), shred the
   config, and restart the unit, which now auto-logins from the saved file

After this runs once, the terminal reconnects on its own across restarts.
"""

from __future__ import annotations

from . import auth, scripts, ssh
from .errors import ConfirmationRequiredError, Mt4ctlError
from .models import Env, Registry

LOGIN_INI = "mt4ctl-login.ini"


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
    ini_rel = scripts.sh_quote(LOGIN_INI)
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

INI="$WORKDIR/"{ini_rel}
ACCFILE="$DDIR/config/accounts.ini"
BEFORE=$(stat -c %Y "$ACCFILE" 2>/dev/null || echo 0)

umask 077
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
WINEPREFIX="$WP" DISPLAY="$DISP" setsid wine terminal.exe /portable {ini_rel} >/dev/null 2>&1 &
PGID=$!

ok=0
for i in $(seq 1 {wait_seconds}); do
  AFTER=$(stat -c %Y "$ACCFILE" 2>/dev/null || echo 0)
  if [ "$AFTER" -gt "$BEFORE" ]; then ok=1; break; fi
  sleep 1
done

kill -TERM -"$PGID" 2>/dev/null
sleep 2
kill -KILL -"$PGID" 2>/dev/null
shred -u "$INI" 2>/dev/null || rm -f "$INI"

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

    # Stop the unit so the one-shot owns the terminal slot.
    await ssh.run(
        host, scripts.build_control_script(term.service, "stop"), root=True, check=False
    )
    script = build_login_script(
        term.service,
        term.data_dir,
        login_account,
        server,
        secret,
        wait_seconds=wait_seconds,
    )
    result = await ssh.run(host, script, timeout=wait_seconds + 30, check=False)
    # Bring the unit back; it auto-logins from the now-saved accounts.ini.
    await ssh.run(
        host, scripts.build_control_script(term.service, "start"), root=True, check=False
    )

    ok = "LOGIN|ok=1" in result.stdout
    if ok:
        return (
            f"{terminal_id}: logged in to account {login_account} on {server}; "
            f"credentials saved, unit restarted for auto-reconnect."
        )
    return (
        f"{terminal_id}: login did not confirm within {wait_seconds}s "
        f"(account {login_account} on {server}). Check `mt4_logs` and verify the "
        f"server name and password."
    )
