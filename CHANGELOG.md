# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `mt4_ea_list` — inventory of experts (strategies) attached per terminal.
- `mt4_autotrading` — terminal master AutoTrading switch (from `terminal.ini`)
  plus per-EA live-trading status (best-effort decode of the chart-expert flags).
- `mt4_info` — terminal build, broker server, and last broker ping (from logs).

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

[Unreleased]: https://github.com/ak40u/mt4ctl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ak40u/mt4ctl/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ak40u/mt4ctl/releases/tag/v0.1.0
