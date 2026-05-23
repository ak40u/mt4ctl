# Installing a headless MetaTrader 4 terminal on Windows via WSL2

This guide runs MetaTrader 4 terminals **headless inside WSL2** on a Windows
machine — Wine + Xvfb + `systemd`, managed entirely over SSH like a Linux box,
with no native Windows GUI. It removes Windows' limits on concurrent terminals
and lets [`mt4ctl`](../README.md) drive the farm the same way it drives a Linux
host.

Why WSL2 instead of native Windows MT4: you get real `systemd` supervision, one
SSH management surface, and no per-Windows-user terminal cap. The terminals run
under Wine inside Ubuntu-in-WSL — identical to the
[Ubuntu setup](install-linux-ubuntu.md), with a Windows wrapper around it.

> Conventions: WSL distro `Ubuntu-24.04`, Linux user `trader`, terminals under
> `~/mt4/`, X display `:99`, broker `ExampleBroker-Demo`. Substitute your own.

---

## 1. Install WSL2 (Windows side, admin)

In an **elevated** PowerShell:

```powershell
wsl --install -d Ubuntu-24.04
```

This enables the WSL + Virtual Machine Platform features and installs Ubuntu.
**Reboot** when prompted (a feature install often returns exit code `3010` =
reboot required). After reboot, launch "Ubuntu-24.04" once to create your Linux
user (`trader`), then you can do everything else over SSH.

Confirm:

```powershell
wsl -l -v          # Ubuntu-24.04 should be VERSION 2, STATE Running
wsl --version      # WSL 2.x, recent kernel
```

If `wsl --install` can't elevate over a remote/SSH session, run the feature
enable as SYSTEM via a scheduled task, then reboot — but the interactive
elevated PowerShell above is by far the simplest.

## 2. Enable systemd inside WSL

WSL2 supports `systemd` but it's off by default. Inside the distro
(`wsl -d Ubuntu-24.04`), create `/etc/wsl.conf`:

```ini
[boot]
systemd=true

[user]
default=trader
```

Then from Windows:

```powershell
wsl --shutdown
```

Relaunch the distro and verify `systemd` is PID 1:

```bash
systemctl is-system-running     # 'running' or 'degraded' is fine
```

## 3. Set up the Wine + Xvfb stack inside WSL

From here you are inside Ubuntu-in-WSL — follow the
[Ubuntu guide](install-linux-ubuntu.md) for the substance. In short:

```bash
sudo dpkg --add-architecture i386
sudo apt update
sudo apt install -y wine winbind xvfb fluxbox x11vnc x11-utils xdotool \
                    imagemagick scrot ttf-mscorefonts-installer cabextract curl
export WINEPREFIX="$HOME/.wine" WINEARCH=win64
wineboot --init
```

Place each terminal's portable folder under `~/mt4/` (e.g. `~/mt4/demo1`), and
create the `xvfb.service` + per-terminal `mt4-demoN.service` units exactly as in
the Ubuntu guide. Enable them:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb.service
sudo systemctl enable --now mt4-demo1.service
```

## 4. Fonts (the smiley gotcha) — and a Windows shortcut

As on Linux, Wine's bundled `wingding.ttf` is a 9 KB stub with no glyphs, so the
MT4 expert smiley shows as an empty box. On Windows + WSL you have the **real
fonts right there** on the C: drive — copy them straight in:

```bash
# WSL can read the Windows filesystem under /mnt/c
cp /mnt/c/Windows/Fonts/wingding.ttf  "$WINEPREFIX/drive_c/windows/Fonts/"
cp /mnt/c/Windows/Fonts/arial*.ttf    "$WINEPREFIX/drive_c/windows/Fonts/" 2>/dev/null
cp /mnt/c/Windows/Fonts/tahoma*.ttf   "$WINEPREFIX/drive_c/windows/Fonts/" 2>/dev/null
cp /mnt/c/Windows/Fonts/cour*.ttf     "$WINEPREFIX/drive_c/windows/Fonts/" 2>/dev/null
ls -l "$WINEPREFIX/drive_c/windows/Fonts/wingding.ttf"   # ~80 KB, not ~9 KB
```

Restart the terminals after adding fonts so they pick them up.

## 5. SSH access to WSL

To manage the farm remotely (and let `mt4ctl` reach it), run an SSH server.
Easiest is OpenSSH **inside WSL** on a non-default port, forwarded from Windows;
or use the Windows OpenSSH server with a default-shell that drops into WSL. Set
up key-based auth and add a host alias in your `~/.ssh/config`:

```
Host my-demo-box
    HostName <windows-host-or-tailscale-name>
    User trader
    # ... your key, port, etc.
```

`mt4ctl` then treats it as a `kind: wsl` host (it wraps remote commands in
`wsl -d Ubuntu-24.04 -- ...`). Example registry entry:

```yaml
hosts:
  my-demo-box:
    ssh: my-demo-box
    kind: wsl
    wsl_distro: Ubuntu-24.04
    broker_host: demoUK.example.com
```

## 6. Start WSL automatically on Windows boot

A WSL distro only runs while it has a live process or session, and **WSL2 shuts
the VM down on idle** — which takes your whole farm down with it. A task that just
*boots* the distro (`wsl … /bin/true`) is **not enough**: it exits immediately, so
once the VM idles it stops and every terminal dies until something pokes it again.

Use a **keep-alive** that *holds* the VM up. Create a Windows **Scheduled Task**:

- Trigger: **At log on** (the task must run in the distro owner's user context —
  WSL distros are per-user, so a `SYSTEM` task usually can't see `Ubuntu-24.04`)
- Action: `wsl.exe -d Ubuntu-24.04 -- /usr/bin/sleep infinity` (a never-ending
  process keeps the VM alive; with `systemd=true` your enabled services stay up)
- Run only when that user is logged on (or "whether logged on or not" if you can
  store the password and the box auto-logs-in)

Test by rebooting and confirming services are still up over SSH **after the VM has
been left idle for a few minutes** — not just right after boot. Autostart is
finicky about user context; verify on a real reboot.

> Don't rely on the terminals themselves to keep WSL alive — the idle timer can
> still win. And a stray `wsl --shutdown` kills the keep-alive (and the farm); if
> you run one, re-trigger the keep-alive task afterward.

If you're **migrating from native Windows MT4**, disable the old autostart
(Startup folder `.bat`/`.lnk`, or its scheduled task) so the two don't both run.

## 7. First login + verify (mt4ctl)

```bash
mt4ctl doctor        # SSH ok, tools present, units found, data dirs present
mt4ctl status        # demo1 -> active / down (not logged in yet)
```

Log each terminal in once with the `mt4_login` tool (headless; credentials are
machine-bound, so this is a one-time bootstrap), then:

```bash
mt4ctl status        # demo1 -> active / up
```

After the one-time login, each terminal auto-reconnects on restart and Windows
reboot — no plaintext password stored on disk.

## 8. VNC from Windows or a Mac (optional)

```bash
# inside WSL — note: WSL sets a Wayland session var that confuses x11vnc, so
# start it with a clean environment:
env -i DISPLAY=:99 x11vnc -localhost -rfbport 5900 -nopw -forever -shared &
```

Tunnel and view:

```bash
ssh -L 5901:127.0.0.1:5900 my-demo-box -N     # then VNC viewer -> localhost:5901
```

(On macOS, the built-in Screen Sharing grabs port 5900 and only does one-way
clipboard — use a standalone VNC viewer and a forwarded port like 5901.)

## Gotchas specific to WSL2

- **Black window content in screenshots** — you need fluxbox (a WM); Xvfb alone
  won't composite Wine windows. With several terminals on one display, raising
  the target window (which `mt4ctl screenshot` does) is what gives a clean grab.
- **Empty smiley** — the Wingdings stub again; copy the real one from
  `/mnt/c/Windows/Fonts` (step 4).
- **`x11vnc` says "Wayland display server detected"** — WSL sets
  `XDG_SESSION_TYPE=wayland`; launch x11vnc with `env -i DISPLAY=:99 ...`.
- **Services don't come back after a Windows reboot** — the WSL autostart task
  didn't fire; re-check the scheduled task's user context (step 6).
- **The whole farm cycles / terminals restart on their own every minute or two** —
  WSL2's idle shutdown is taking the VM down whenever it's idle, and a boot-only
  autostart (`/bin/true`) doesn't hold it. Use a `sleep infinity` keep-alive task
  (step 6). A stray `wsl --shutdown` also kills the keep-alive — re-run the task.
- **Spurious Wine freezes on broker reconnect** — same as on bare Linux; a
  heartbeat-file + `systemd` watchdog timer that restarts a stale terminal is the
  production-grade fix.

---

See also: [Installing on Ubuntu](install-linux-ubuntu.md) ·
[Configuration](configuration.md) · [Tools](tools.md)
