# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.6.0] - 2026-05-24

Findings from a real 7-terminal deploy: verify is now trustworthy, and a few
sharp edges from that run are smoothed.

### Added

- `mt4_verify` tool / `mt4ctl verify <terminal>` — a standalone, report-only
  health gate that **polls** a terminal until it is healthy (service active +
  broker connected) or a timeout, reporting the terminal's state at timeout.
  Reusable after any restart, not just a deploy.
- `deploy --reset-market-watch` (and the `reset_market_watch` tool arg) — deletes
  `history/*/symbols.sel` inside the existing stopped window (backed up first) so
  MT4 rebuilds Market Watch (broker default set + loaded chart symbols) on the
  deploy's own start, capping unbounded symbol carry-over. Forces a stop/start
  cycle even with no file changes. No binary write — `symbols.sel` is never
  authored, only deleted while the terminal is down.
- `deploy --verify-timeout` (and the `verify_timeout` tool arg) to tune the
  post-restart verify poll window.

### Changed

- **Deploy verify now polls instead of taking a single post-start snapshot.** A
  real terminal needs ~30–90 s to reconnect and a minute to load a large EA set,
  so the old one-shot check reported essentially every healthy deploy as "verify
  NOT confirmed". Verify now retries until the terminal is healthy or the window
  (~120 s, configurable) elapses, then reports the state at timeout — so a genuine
  failure is distinguishable from normal startup timing. (No-restart health
  confirmations still take one snapshot.)
- Verify EA reporting is summarized by count (`N/total experts loaded, M pending`)
  instead of dumping the full not-yet-loaded list; names are listed only for
  **errored** experts (the actionable set).
- The unmanaged-overwrite refusal is now actionable for chart conflicts: a `.chr`
  conflict that is a live foreign chart (e.g. a watchdog) tells the operator to
  renumber the bundle's charts around the slot, or include/adopt the chart.
- `mt4_status` distinguishes a just-restarted **connecting** terminal (broker
  reconnect in progress, cached creds will return on their own) from a persistent
  **down** state, instead of always suggesting `mt4_login`. Adds an
  `active_enter_seconds` (unit uptime) field to the status protocol/`TerminalStatus`.
- `deploy` output notes that a `.chr` shown as `update` shortly after a restart can
  be cosmetic drift (MT4 rewrites `.chr` on exit) — the same heads-up `adopt`
  already gives.

## [0.5.1] - 2026-05-24

### Added

- `mt4_adopt` now reports the live charts on the host that are **not** in the
  bundle (left foreign), so the operator can see at a glance what adopt left
  untouched (e.g. a watchdog's chart). Transparency only — it reports, never
  touches. New `AdoptResult.foreign` field, surfaced by the tool/CLI output.

### Fixed

- `deploy`/`adopt` failed on real-size bundles over a WSL host with `cmd.exe`'s
  "The command line is too long". The SSH transport inlined the base64 of the
  whole generated script into the remote command, whose length grows with the
  file count (~140 managed files → ~13 KB base64 → past the Windows ~8 KB
  command-line limit). The script is now streamed over **stdin** (`base64 -d |
  bash`), making the command fixed-size regardless of bundle size — the same
  stdin path the binary tar upload already proved byte-clean. Verified on a real
  WSL host with a 142-file bundle.

## [0.5.0] - 2026-05-23

### Added

- `mt4_adopt` — take an already-running ("brownfield") terminal under management:
  the first cutover. Records the bundle's footprint into `.mt4ctl/deployed.json` at
  the files' current on-disk hashes so a subsequent `mt4_deploy` reconciles from
  that baseline. Records-only — no upload, no restart, no preview; bundle-scoped
  (foreign files like a watchdog's chart stay foreign); refuses if any bundle file
  is absent on the host. Exposed via the `mt4_adopt` MCP tool and `mt4ctl adopt`
  CLI. Adds a canonical `deploy.build_manifest` serializer (the inverse of
  `parse_manifest`, parity-tested against the deploy apply path).

### Fixed

- `docs/deploy.md`: corrected the file-ownership rationale — MetaTrader rewrites
  its `.chr` files on exit, but `deployed.json` is mt4ctl's own manifest (never
  touched by MetaTrader); it is kept unit-owned so the next mt4ctl run can rewrite it.

## [0.4.0] - 2026-05-23

### Added

- `mt4_deploy` — idempotent, managed-subset strategy deploy ("kubectl-apply for
  one terminal"): push a local bundle of ready `.chr` charts + `.ex4` experts and
  reconcile a terminal to it. Touches only what mt4ctl deployed (tracked in
  `.mt4ctl/deployed.json`); foreign files (e.g. a watchdog's chart) are left
  untouched, and a bundle file that would overwrite an unmanaged file is refused.
  Write order is **stop → drain → backup → apply → start** (MT4 rewrites `.chr` on
  exit, so nothing is written until `terminal.exe` is gone, and a stopped terminal
  is always restarted); files are written as the unit's own user; a per-terminal
  lockdir serializes concurrent deploys. A pre-apply backup is kept and restored
  internally on apply failure (no `mt4_rollback` — recover by re-deploying the
  previous bundle). Verify is report-only (service + broker + EA-load lines from
  the log). Exposed via the `mt4_deploy` MCP tool and `mt4ctl deploy` CLI.
- `ssh.put_tar` — binary-safe SSH upload primitive (raw tar streamed to a remote
  `tar -x` over stdin), verified byte-identical through the WSL `cmd.exe → wsl.exe
  → bash` chain.

## [0.3.0] - 2026-05-23

### Added

- `mt4_ea_list` — inventory of experts (strategies) attached per terminal.
- `mt4_autotrading` — terminal master AutoTrading switch (from `terminal.ini`)
  plus per-EA live-trading status (best-effort decode of the chart-expert flags).
- `mt4_info` — terminal build, broker server, and last broker ping (from logs).

### Fixed

- Fan-out tools (`mt4_ea_list`/`mt4_autotrading`/`mt4_info`) no longer blank the
  whole `all` result when a single host is unreachable — each terminal renders
  its own error row (matching `mt4_status`).

## [0.2.0] - 2026-05-23

### Added

- Setup CLI: `mt4ctl init` (write a starter registry), `mt4ctl list` (offline),
  and `mt4ctl doctor` (check registry, SSH, remote tools, units, data dirs);
  `serve` remains the default with no subcommand.
- `mt4_doctor` MCP tool exposing the same diagnostics to the agent.
- `mt4_status` now explains *why* a terminal is unhealthy (e.g. "no broker
  connection") and ends with grouped **next steps** (e.g. run `mt4_login`).
- `doctor`/`mt4_doctor` now check broker-connection health and warn when
  terminals are active but not connected, instead of reporting "all passed".
- Host setup guides for Ubuntu and Windows/WSL2 under `docs/`.

### Fixed

- `ssh`: the base64 remote wrapper runs under `set -o pipefail` (fails closed if
  the decoder is missing/broken); local `ssh` spawn failures become a clean
  `RemoteCommandError`.
- `status`/`control`: capture `systemctl is-active` without the trailing
  `|| echo unknown` that corrupted the line protocol for `inactive`/`failed`
  units.
- `login`: build and validate the bootstrap before stopping the unit, and
  restart it in a `finally` so a validation error or timeout never leaves it
  stopped.
- `doctor`: verify the probe reported every core tool and terminal — a truncated
  probe fails closed instead of reporting a green result; all configured hosts
  are probed.
- `init`: create the registry atomically at mode `600`.

## [0.1.0] - 2026-05-23

Initial release.

### Added

- MCP stdio server exposing six tools: `mt4_list`, `mt4_status`, `mt4_logs`,
  `mt4_screenshot`, `mt4_control`, `mt4_login`.
- YAML registry with validation; supports native Linux and WSL2 hosts.
- Concurrent, per-terminal status with cgroup-based broker-connection attribution
  (gated on root or matching service user, otherwise reported as unknown).
- Headless first-login bootstrap with process-group-scoped cleanup and
  credential shredding via a cleanup trap.
- Live-trading guardrails (`confirm=true`) on mutating operations.
- Credential resolution chain (argument → env var → secrets file) with a
  permission check on the secrets file.
- Human-friendly entry point (`--help`/`--version` and a TTY guard) plus
  fail-fast config resolution and actionable SSH-failure errors.
- Test suite, strict `mypy`, `ruff`, and a 3.11–3.13 CI matrix.

[Unreleased]: https://github.com/ak40u/mt4ctl/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ak40u/mt4ctl/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/ak40u/mt4ctl/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ak40u/mt4ctl/releases/tag/v0.1.0
