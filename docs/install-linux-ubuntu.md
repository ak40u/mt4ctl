# Installing a headless MetaTrader 4 terminal on Ubuntu

This guide sets up a MetaTrader 4 terminal to run **headless** on a fresh Ubuntu
server — Wine under a virtual X display, supervised by `systemd`, no desktop.
It's the host setup that [`mt4ctl`](../README.md) is built to manage.

What you'll end up with:

```
systemd ─┬─ xvfb.service        Xvfb :99 + a tiny window manager (fluxbox)
         └─ mt4-term1.service   wine terminal.exe /portable  (on DISPLAY :99)
```

Then `mt4ctl` drives it over SSH: status, logs, screenshots, lifecycle, and the
one-time headless login.

> Conventions below: user `trader`, terminal at `~/mt4/term1`, X display `:99`,
> broker server `ExampleBroker-Demo`, account `1000001`. Substitute your own.

---

## 1. System packages

```bash
sudo dpkg --add-architecture i386          # 32-bit deps for Wine (MT4 is 32-bit)
sudo apt update
sudo apt install -y \
  wine winbind \
  xvfb fluxbox x11vnc x11-utils xdotool \
  imagemagick scrot \
  cabextract curl
```

- **wine** runs `terminal.exe`. **winbind** prevents Wine login/network warnings.
- **xvfb** is the headless X server; **fluxbox** is a minimal window manager —
  without a WM, Wine windows don't composite and screenshots come out black.
- **x11vnc** lets you peek at the display when needed.
- **imagemagick** (`import`) / **scrot** + **xdotool** are what `mt4ctl
  screenshot` shells out to.

For a newer Wine, add the [WineHQ repo](https://wiki.winehq.org/Ubuntu) instead
of the distro package — the distro `wine` is fine for MT4 in most cases.

## 2. Create the Wine prefix

```bash
export WINEPREFIX="$HOME/.wine"
export WINEARCH=win64                       # win64 prefix runs 32-bit MT4 fine
wineboot --init
winecfg                                     # set "Windows Version" to Windows 10, close
```

One prefix can host **several** terminals (they each run from their own portable
folder). That's the common single-user farm layout.

## 3. Fonts — the smiley gotcha

MT4 draws the "expert smiley" (and several UI glyphs) with **Wingdings**. Wine
ships a 9 KB *stub* `wingding.ttf` with no glyphs, so the smiley renders as an
empty box and you can't visually confirm an EA is attached.

Install the core fonts and drop in a **real** Wingdings:

```bash
sudo apt install -y ttf-mscorefonts-installer
mkdir -p "$WINEPREFIX/drive_c/windows/Fonts"

# Copy the genuine Wingdings.ttf (~82 KB) from any Windows box
# (C:\Windows\Fonts\wingding.ttf) into the prefix:
scp you@windows-box:'C:/Windows/Fonts/wingding.ttf' \
    "$WINEPREFIX/drive_c/windows/Fonts/wingding.ttf"
```

Also copy `arial.ttf`, `tahoma.ttf`, `couri.ttf` from a Windows machine for
clean MT4 text. Verify the Wingdings file is the real one, not the stub:

```bash
ls -l "$WINEPREFIX/drive_c/windows/Fonts/wingding.ttf"   # expect ~80 KB, not ~9 KB
```

## 4. Install the terminal (portable)

MT4 is broker-distributed. The clean headless approach is a **portable** copy:
get a terminal folder containing `terminal.exe` (e.g. from your broker's
installer, or copy an existing terminal directory) and place it at
`~/mt4/term1`. Running with `/portable` keeps all data inside that folder.

```bash
mkdir -p ~/mt4/term1
# copy your broker's terminal.exe (+ any bundled files) into ~/mt4/term1
```

> Do not commit broker binaries or your account credentials anywhere.

## 5. systemd: the display + the terminal

The display service (`/etc/systemd/system/xvfb.service`):

```ini
[Unit]
Description=Xvfb + fluxbox virtual display :99
After=network.target

[Service]
User=trader
ExecStartPre=-/bin/rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
ExecStart=/bin/sh -c '/usr/bin/Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp -ac & sleep 2; DISPLAY=:99 /usr/bin/fluxbox & wait'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The terminal service (`/etc/systemd/system/mt4-term1.service`):

```ini
[Unit]
Description=MT4 terminal term1
After=xvfb.service network-online.target
Wants=network-online.target
Requires=xvfb.service

[Service]
User=trader
Environment=HOME=/home/trader
Environment=DISPLAY=:99
Environment=WINEPREFIX=/home/trader/.wine
WorkingDirectory=/home/trader/mt4/term1
ExecStart=/usr/bin/wine terminal.exe /portable
Restart=on-failure
RestartSec=30
KillMode=mixed
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb.service
sudo systemctl enable --now mt4-term1.service
```

> `mt4ctl` reports per-terminal connection by intersecting the unit's cgroup
> with broker sockets, and it needs to *see* the terminal's process — so run the
> unit as the same user you SSH in as (or query as root). Keep `User=` matching
> your SSH user for the cleanest experience.

## 6. First login (headless, with mt4ctl)

MetaTrader encrypts its saved password with a machine-bound key, so a freshly
copied terminal must authenticate **once** on this host. Add the host + terminal
to your `mt4ctl` registry (`mt4ctl init` to start one), then:

```bash
mt4ctl doctor                       # confirm SSH, tools, unit, data dir
mt4ctl status                       # term1 -> active / down (not logged in yet)
```

Log it in from your MCP client (Claude) with the `mt4_login` tool, or wire the
password via `MT4CTL_PASSWORD_1000001` and run it. After it succeeds the unit
auto-reconnects on every restart — no plaintext password is stored on disk.

Alternatively, log in once manually over VNC (next section), then `mt4ctl` takes
over.

## 7. Peeking with VNC (optional)

```bash
# on the server
DISPLAY=:99 x11vnc -localhost -rfbport 5900 -nopw -forever -shared &
# from your laptop
ssh -L 5901:127.0.0.1:5900 trader@server -N
# then point a VNC viewer at localhost:5901
```

Useful for the very first manual login or to eyeball the charts. `mt4ctl
screenshot` covers most day-to-day needs without a viewer.

## 8. Verify

```bash
mt4ctl status        # term1 -> active / up
mt4ctl doctor        # all checks passed
```

## Troubleshooting

- **Smiley / glyphs are empty boxes** — the Wingdings stub is still in place; see
  step 3 and confirm the file is ~80 KB.
- **Black screenshots** — fluxbox isn't running; Xvfb alone doesn't composite
  windows. Check `xvfb.service`.
- **`mt4_status` shows `?` (connection unknown)** — the status user can't see the
  terminal's sockets. Run the unit as your SSH user, or query as root.
- **Terminal connects then drops on weekends** — demo accounts idle-disconnect
  when markets are closed; it reconnects on the next session. Not a fault.
- **Spurious freezes after broker reconnect** — a known Wine quirk; a `systemd`
  watchdog that restarts the unit when a heartbeat file goes stale is the robust
  fix for production. (A small EA writing a heartbeat + a `systemd` timer that
  restarts on staleness is the pattern.)

---

Next: [Installing on Windows via WSL2](install-windows-wsl.md) for the same stack
inside WSL.
