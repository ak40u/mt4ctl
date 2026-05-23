<div align="center">

# mt4ctl

**An MCP server for operating headless MetaTrader terminals — over SSH, from your agent.**

Manage MetaTrader 4 terminals running under **Wine + systemd** on remote hosts
(native Linux *or* WSL2) entirely through the [Model Context Protocol](https://modelcontextprotocol.io):
check status, read logs, capture screenshots, control the systemd lifecycle, and
perform the tricky **headless first-login** — all as clean, typed tools.

[![CI](https://github.com/ak40u/mt4ctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ak40u/mt4ctl/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mt4ctl)](https://pypi.org/project/mt4ctl/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-7C3AED)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

## Why

Algo traders increasingly run MetaTrader 4 **headless on Linux** — Wine under
`Xvfb`, supervised by `systemd`, no GUI. That's great for uptime and terrible for
day-to-day operations: every "is it connected?", "restart that one", or "log this
new account in" turns into a fragile chain of
`ssh → (Windows cmd → wsl) → bash → systemctl → wine`, with quoting hazards at
every hop.

`mt4ctl` collapses that chain into a handful of MCP tools. Point it at a registry
of your hosts and terminals, wire it into Claude (or any MCP client), and operate
the whole farm conversationally:

> *"Which demo terminals are down?"* · *"Restart demo2."* ·
> *"Log demo2 into account 1000002 on ExampleBroker-Demo."* ·
> *"Screenshot the live terminal so I can see the AutoTrading state."*

## Quickstart (5 minutes)

The `init` → `list` → `doctor` commands let you set up and verify everything
**before** wiring an MCP client:

```bash
# 1. write a starter registry, then fill in your hosts + terminals
uvx mt4ctl init                 # creates ~/.config/mt4ctl/terminals.yaml
$EDITOR ~/.config/mt4ctl/terminals.yaml

# 2. verify — offline, then over SSH (no MCP client needed)
uvx mt4ctl list                 # confirms the registry parses
uvx mt4ctl doctor               # checks SSH, remote tools, units, data dirs

# 3. wire into Claude Code
claude mcp add --scope user mt4ctl \
  --env MT4CTL_CONFIG="$HOME/.config/mt4ctl/terminals.yaml" \
  -- uvx mt4ctl
```

Then ask Claude: **"Use mt4_list to show my configured terminals,"** then
**"mt4_status,"** and **"mt4_doctor"** if anything looks off. Full setup and
other clients are below.

## Features

- **Per-terminal connection detection** — attributes established broker sockets
  to each terminal's `systemd` cgroup, so terminals sharing a host (and a Wine
  prefix) are reported independently — not guessed from a host-wide count.
- **Headless first-login** — automates the one-time bootstrap a migrated terminal
  needs (MetaTrader's saved password is machine-bound), then hands control back
  to `systemd` for automatic reconnection on every restart.
- **Idempotent strategy deploy** — *kubectl-apply for one terminal*: push a local
  bundle of charts + experts and reconcile a terminal to it, touching only what
  mt4ctl deployed (foreign files like a watchdog's chart stay untouched), with a
  backup-and-restore-on-failure apply and a report-only health verify.
- **Native *and* WSL2 hosts** — one registry, two execution models; commands are
  base64-shipped so nothing breaks in the `cmd.exe → wsl.exe → bash` gauntlet.
- **Live-trading guardrails** — terminals tagged `env: live` reject mutating
  operations unless you pass `confirm=true`.
- **Concurrent status** — hosts are polled in parallel via `asyncio`.
- **Secrets stay secret** — passwords resolve from arg → env → secrets file,
  are never logged, and the transient remote login config is `shred`-ed after use.

## How it works

```
┌────────────┐   MCP/stdio   ┌──────────────────┐
│ MCP client │ ────────────► │     mt4ctl       │
│ (Claude…)  │               │  FastMCP server  │
└────────────┘               └────────┬─────────┘
                                       │ asyncio SSH (base64-framed)
                 ┌─────────────────────┼─────────────────────┐
                 ▼                                           ▼
        ┌─────────────────┐                        ┌──────────────────┐
        │  native Linux   │                        │  Windows + WSL2  │
        │  sudo systemctl │                        │  wsl -u root --  │
        ├─────────────────┤                        ├──────────────────┤
        │ mt4-live-main…  │  systemd units running │ mt4-demo1…       │
        │ wine terminal.exe (Xvfb display)         │ wine terminal.exe│
        └─────────────────┘                        └──────────────────┘
```

A thin, typed core (`models` → `config` → `ssh` → `scripts` → `deploy` →
`operations`/`login`) sits under the `server` adapter, so the logic is testable
without a network and the MCP layer stays a one-line-per-tool shell.

## Install

The fastest path needs no clone and no global install — [`uv`](https://docs.astral.sh/uv/)
runs `mt4ctl` straight from the repo and fetches a matching Python itself:

```bash
uvx mt4ctl   # runs the stdio server
```

No `uv` yet? `curl -LsSf https://astral.sh/uv/install.sh | sh` — or skip it and use
the `pipx` path below.

Prefer a persistent `mt4ctl` command? Install it with `uv` or `pipx`:

```bash
uv tool install mt4ctl
# or
pipx install mt4ctl
```

For development:

```bash
git clone https://github.com/ak40u/mt4ctl.git && cd mt4ctl
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
```

The server machine needs either `uv` or Python 3.11+, plus SSH access to your
hosts. The remote hosts need the usual tools `mt4ctl` shells out to: `systemctl`,
`ss`, `getent`, and (for screenshots) `imagemagick`/`scrot` + `xdotool`.

## Configure

Copy the example registry and fill in your real hosts and terminals:

```bash
mkdir -p ~/.config/mt4ctl
cp examples/terminals.example.yaml ~/.config/mt4ctl/terminals.yaml
```

The registry is resolved from `MT4CTL_CONFIG`, then
`~/.config/mt4ctl/terminals.yaml`, then `./terminals.yaml`. See
[`examples/terminals.example.yaml`](examples/terminals.example.yaml) for the full
schema and [`docs/configuration.md`](docs/configuration.md) for details.

> **Keep your populated registry private.** It maps your accounts and
> infrastructure. The default `.gitignore` excludes `terminals.yaml`.

## Setting up terminal hosts

`mt4ctl` manages terminals; it doesn't install them. To stand up a host that runs
MT4 headless (Wine + Xvfb + `systemd`) so `mt4ctl` has something to drive:

- **[Ubuntu / Linux server](docs/install-linux-ubuntu.md)** — Wine, the Xvfb +
  window-manager display, fonts (incl. the real Wingdings the MT4 smiley needs),
  `systemd` units, and the one-time headless login.
- **[Windows via WSL2](docs/install-windows-wsl.md)** — the same stack inside
  WSL2, plus enabling WSL + `systemd`, copying fonts from the Windows C: drive,
  boot autostart, and the WSL-specific gotchas.

## Connect to an MCP client

**Claude Code** — one command wires it up (user scope = available in every project):

```bash
claude mcp add --scope user mt4ctl \
  --env MT4CTL_CONFIG="$HOME/.config/mt4ctl/terminals.yaml" \
  -- uvx mt4ctl
```

Or commit a project `.mcp.json` to share with a team (Claude Code expands `${HOME}`):

```json
{
  "mcpServers": {
    "mt4ctl": {
      "command": "uvx",
      "args": ["mt4ctl"],
      "env": { "MT4CTL_CONFIG": "${HOME}/.config/mt4ctl/terminals.yaml" }
    }
  }
}
```

**Claude Desktop** — Settings → Developer → Edit Config (`claude_desktop_config.json`),
same shape but use an **absolute** config path (Desktop does not expand `${HOME}`),
and an absolute `command` path if `uvx` is not on the GUI app's `PATH` (`which uvx`):

```json
{
  "mcpServers": {
    "mt4ctl": {
      "command": "uvx",
      "args": ["mt4ctl"],
      "env": { "MT4CTL_CONFIG": "/Users/you/.config/mt4ctl/terminals.yaml" }
    }
  }
}
```

> Installed `mt4ctl` persistently (uv/pipx)? Replace `command`/`args` with just
> `"command": "mt4ctl"`.

## Tools

| Tool | Mutates | Description |
| --- | :---: | --- |
| `mt4_list` | – | List configured terminals (offline). |
| `mt4_status` | – | Per-terminal service state + broker connection + log age. |
| `mt4_logs` | – | Tail / grep a terminal's newest log file. |
| `mt4_screenshot` | – | Capture a terminal window as PNG. |
| `mt4_control` | ✓ | `start` / `stop` / `restart` a unit (live needs `confirm`). |
| `mt4_login` | ✓ | One-time headless login for auto-reconnect (live needs `confirm`). |
| `mt4_doctor` | – | Diagnose registry, SSH, remote tools, units, and data dirs. |
| `mt4_ea_list` | – | List the experts (strategies) attached per terminal. |
| `mt4_autotrading` | – | AutoTrading master switch + per-EA live-trading status. |
| `mt4_info` | – | Terminal build, broker server, and last broker ping. |
| `mt4_deploy` | ✓ | Reconcile a terminal to a local strategy bundle (live needs `confirm`). |
| `mt4_adopt` | ✓ | Record an already-running bundle as managed — the brownfield first cutover. |

Full reference: [`docs/tools.md`](docs/tools.md).

## CLI

Besides serving MCP, `mt4ctl` has a few commands for setup and debugging without
a client:

```bash
mt4ctl init [path]   # write a starter terminals.yaml (default: XDG config path)
mt4ctl list          # list configured terminals (offline)
mt4ctl doctor        # check registry, SSH, remote tools, units, data dirs
mt4ctl deploy <terminal> <bundle> [--dry-run] [--confirm]   # apply a strategy bundle
mt4ctl adopt <terminal> <bundle> [--confirm]                # adopt an already-running farm
mt4ctl serve         # run the MCP stdio server (the default with no subcommand)
```

## Deploy

Push a local **bundle** of charts + experts onto a terminal and reconcile it to
that desired set — idempotently, touching only what mt4ctl deployed. The bundle
mirrors the MT4 layout:

```
<bundle>/
  profiles/default/<name>.chr        # ready charts (one expert each)
  MQL4/Experts/<folder>/<ea>.ex4     # the experts those charts reference
```

```bash
mt4ctl deploy demo3 ./bundle --dry-run   # preview the add/update/remove/foreign plan
mt4ctl deploy demo3 ./bundle             # apply (env=live terminals need --confirm)
```

It is **apply-only** (no selection, lot sizing, chart generation, or compilation —
you build the bundle), idempotent (a re-run is a no-op that still verifies health),
and managed-subset (foreign files like a watchdog's chart are never touched). The
write order is **stop → drain → backup → apply → start**, verify is **report-only**,
and there is no rollback command — recovery is to re-deploy the previous bundle.

Already running strategies on the farm? Take it under management first with
`mt4ctl adopt <terminal> <bundle>` (records the current footprint, changes
nothing), then deploy as usual. Full model and caveats: [`docs/deploy.md`](docs/deploy.md).

## Security

- Mutations on `env: live` terminals require explicit `confirm=true`.
- Credentials resolve from argument → `MT4CTL_PASSWORD_<account>` →
  secrets file; they are never written to logs and the transient remote login
  config is shredded after use.
- All remote execution goes through your existing SSH config and key-based auth;
  `mt4ctl` stores no credentials of its own.
- During `mt4_login` the password is embedded in the base64-framed script handed
  to `ssh`, so it is briefly visible in the local process list to your own user.
  On the remote side it is written only to a fresh `mktemp` config (mode 600) that
  a cleanup trap `shred`s on any exit path. On POSIX, the local secrets file is
  rejected if it is readable by group/other.

## Deep dive

- **[The MT4 "32 terminals per Windows user" limit](docs/the-32-terminal-limit.md)** —
  reproducing the cap on a clean box, locating the exact kernel object that enforces
  it (a per-instance **Mutant** in the session-local `\Sessions\<id>\BaseNamedObjects`),
  and why running headless under Wine on Linux — what `mt4ctl` drives — sidesteps it
  entirely.

## Development

```bash
ruff check src tests      # lint
mypy                      # type-check (strict)
pytest                    # tests
```

See [`docs/architecture.md`](docs/architecture.md) for the module boundaries.

## License

MIT © Pavel Volkov. See [LICENSE](LICENSE).
