# Tools reference

All tools are exposed over the MCP stdio transport. Read-only tools are always
available; mutating tools (`mt4_control`, `mt4_login`) require `confirm=true` when
the target terminal is tagged `env: live`.

## `mt4_list`

List configured terminals with host, account, and environment. Offline — no SSH.
Call this first to discover terminal ids.

## `mt4_status`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | `"all"` | a terminal id, or `"all"` |

Polls hosts concurrently and returns a table:

```
TERMINAL     ENV   ACCOUNT      SERVICE   CONN   LOG AGE
------------------------------------------------------------
demo1        demo  1000001      active    up     8s
demo2        demo  1000002      active    up     3s
live-main    live  2000001      active    down   41s     <-- check
```

- **SERVICE** — raw `systemctl is-active` value.
- **CONN** — `up` / `down` / `?`, attributed per terminal via its cgroup's
  established `:443` sockets.
- **LOG AGE** — seconds since the newest log file was written.

A terminal is healthy when `SERVICE=active` and `CONN=up`. Unhealthy rows carry a
short reason and the output ends with a grouped **next steps** section. A
just-restarted terminal that is `down` is reported as `connecting (Ns since
restart)` — the broker reconnect is normal and cached credentials return on their
own — and is told apart from a persistent `no broker connection` (which is where
`mt4_login` is the right next step). Use `mt4_verify` to wait one out.

## `mt4_logs`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |
| `pattern` | string | `""` | optional case-insensitive regex |
| `lines` | int | `50` | trailing lines (1–1000) |

Returns the tail of the terminal's newest log file. With `pattern`, greps first —
useful for `login`, `disconnect`, `error`.

## `mt4_screenshot`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |

Captures the terminal's display as a PNG (returned as an MCP image), with the
target window raised and focused first so it isn't obscured. On hosts where each
terminal owns its own X display this is effectively just that terminal; on a
shared display the grab includes the full screen with the target on top.

## `mt4_control` · mutating

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |
| `action` | string | — | `start` \| `stop` \| `restart` |
| `confirm` | bool | `false` | required for `env: live` terminals |

Runs the systemd action (as root) and reports the resulting service + connection
state.

## `mt4_deploy` · mutating

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |
| `bundle` | string | — | **local** bundle directory (read here, pushed over SSH) |
| `dry_run` | bool | `false` | preview the plan; no lock, no upload, no change |
| `confirm` | bool | `false` | required for `env: live` terminals |
| `reset_market_watch` | bool | `false` | delete `symbols.sel` while stopped so MT4 rebuilds Market Watch |
| `verify_timeout` | float | `~120` | seconds to poll post-restart health before reporting |

Reconciles a terminal's managed strategy files to a local bundle, idempotently.
Apply-only: it does not select strategies, set lots/magic, generate charts, or
compile — you hand it finished artifacts. The bundle mirrors the MT4 layout:

```
<bundle>/
  profiles/default/<name>.chr        # ready charts (one expert each)
  MQL4/Experts/<folder>/<ea>.ex4     # the experts those charts reference
```

Always `dry_run=true` first to preview the add/update/remove/foreign plan.
Re-running the same bundle is a no-op ("no changes") but still verifies health.
mt4ctl touches only what **it** deployed (tracked in `.mt4ctl/deployed.json`);
foreign files like a watchdog's chart are left untouched, and a bundle file that
would overwrite an unmanaged file is refused (a foreign chart-slot collision tells
you to renumber the bundle's charts around it). Write order is
**stop → drain → backup → apply → start**, and verify **polls** until healthy or a
timeout, **report-only** (a failed verify does not revert). `reset_market_watch`
rebuilds the Market Watch in the stopped window to cap symbol carry-over (see
[deploy.md](deploy.md#market-watch-reset-optional)). There is no rollback command —
recovery is to re-deploy the previous bundle. See [deploy.md](deploy.md) for the
full model.

## `mt4_adopt` · mutating

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |
| `bundle` | string | — | **local** bundle the terminal already runs |
| `confirm` | bool | `false` | required for `env: live` terminals |

Takes an already-running terminal under management (the brownfield **first
cutover**). On a terminal whose strategies mt4ctl did not place, the first
`mt4_deploy` refuses; run `mt4_adopt` once first to record the bundle's footprint
into `deployed.json` at the files' current on-disk hashes. **Records-only** — no
upload, no restart, no preview (so no `dry_run`). Bundle-scoped (foreign files like
a watchdog's chart stay foreign); every bundle file must already be present on the
host or it refuses. It also **reports any live charts on the host that are not in
the bundle**, so you can see exactly what was left foreign (e.g. a watchdog). After
adopt, `mt4_deploy <t> <bundle> --dry-run` should report "no changes". See
[deploy.md](deploy.md#adopting-an-existing-farm-first-cutover).

## `mt4_verify`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |
| `timeout` | float | `~120` | seconds to poll before reporting |

Polls a terminal until it is healthy (`SERVICE=active` and broker connected) or
the timeout elapses, then reports its state. The same poll routine deploy uses,
exposed standalone so it is useful after **any** restart — it waits out the broker
reconnect instead of taking one snapshot, so a real failure is distinguishable
from normal startup. Read-only.

## `mt4_login` · mutating

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | — | terminal id |
| `server` | string | — | broker server name, e.g. `ExampleBroker-Demo` |
| `account` | string | `""` | login number; defaults to the configured account |
| `password` | string | `""` | explicit password; else resolved from env/secrets |
| `confirm` | bool | `false` | required for `env: live` terminals |

Performs the one-time headless login a migrated terminal needs, then restarts the
unit so it auto-reconnects from the saved (re-encrypted) credentials. See
[architecture.md](architecture.md) for the mechanism.

## `mt4_doctor`

No arguments. Read-only.

Diagnoses the setup: registry, secrets-file permissions, per-host SSH
reachability, required remote tools, systemd units, data directories, and
**broker-connection health** (it warns when terminals are active but not
connected, rather than reporting a misleading "all passed"). Returns a ✓/!/✗
checklist. Use it when a terminal is unexpectedly `unknown` or `mt4_status` looks
wrong. The same checks are available from the shell as `mt4ctl doctor`.

## `mt4_ea_list`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | `"all"` | a terminal id, or "all" |

Read-only. Lists the expert advisors (strategies) attached to a terminal, parsed
from its chart files. For a single terminal it lists every EA; for `"all"` it
shows the count per terminal.

## `mt4_autotrading`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | `"all"` | a terminal id, or "all" |

Read-only. Reports whether algo-trading is enabled, at two levels: the terminal
**master AutoTrading** switch (from `terminal.ini` `Experts=` — authoritative)
and how many attached experts have live-trading enabled. Flags terminals whose
master is off (nothing trades) or whose experts have live-trading disabled.

> The per-EA live-trading flag is a *best-effort* decode of the MT4 chart-expert
> `flags` bitmask (low bit); the terminal master switch is authoritative.

## `mt4_info`

| Arg | Type | Default | Description |
| --- | --- | --- | --- |
| `terminal` | string | `"all"` | a terminal id, or "all" |

Read-only. Reports each terminal's build, broker server, and last broker ping,
parsed from its log — useful to confirm what build/broker a terminal is on and
its connection latency.
