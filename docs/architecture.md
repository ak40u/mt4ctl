# Architecture

`mt4ctl` is a thin MCP adapter over a typed, network-free core. Each module has a
single responsibility and depends only on those below it.

```
server.py        MCP tool definitions (FastMCP) — one thin wrapper per tool
   │
   ├── operations.py   status · logs · screenshot · control  (orchestration)
   ├── login.py        headless first-login bootstrap
   │
   ├── scripts.py      pure bash builders (no I/O) ── the only place shell lives
   ├── ssh.py          async SSH transport (base64-framed), native + WSL
   ├── auth.py         credential resolution chain
   ├── config.py       YAML → Registry, with validation
   ├── models.py       Host · Terminal · Registry · TerminalStatus (dataclasses)
   └── errors.py       typed, actionable exceptions
```

## Design choices

**Pure shell builders.** Every remote command is produced by a pure function in
`scripts.py` (or `login.py`) and executed by `ssh.py`. Because the builders are
side-effect-free, the gnarly shell logic is unit-tested directly, and the
orchestration layer never concatenates commands by hand.

**Base64 framing.** Remote scripts are shipped as
`echo <base64> | base64 -d | bash`. Only the base64 alphabet and pipes ever cross
the `ssh → cmd.exe → wsl.exe → bash` boundary, eliminating an entire class of
quoting bugs. The same trick fetches binary files (screenshots) back over stdout.

**Per-terminal connection attribution.** Naively, "is it connected?" on a host
running ten terminals is ambiguous. `mt4ctl` reads each `systemd` unit's cgroup
PIDs (`/sys/fs/cgroup/.../cgroup.procs`) and counts established `:443` sockets
owned by exactly those PIDs — so terminals sharing a host *and a Wine prefix* are
reported independently.

**Two execution models, one registry.** `HostKind.NATIVE` runs commands directly
and escalates with `sudo`; `HostKind.WSL` wraps them in `wsl -d <distro> --` and
escalates with `wsl -u root`. The difference is isolated to one function in
`ssh.py`.

**Live guardrails in the core.** The `confirm` requirement for `env: live`
terminals lives in `operations`/`login`, not just the tool layer, so any future
adapter (CLI, HTTP) inherits the same safety.

## The login bootstrap

MetaTrader encrypts the saved account password with a machine-bound key, so a
terminal copied to a new host shows an authorization dialog and never connects.
`login.py` automates the recovery:

1. stop the unit (free the terminal slot)
2. write a transient `[Common]` startup config (`login`/`password`/`server`, mode 600)
3. launch the terminal **in its own process group** (`setsid`), reusing the unit's
   `WorkingDirectory` / `WINEPREFIX` / `DISPLAY`
4. wait for `config/accounts.ini` to be rewritten — MetaTrader's signal that
   authentication succeeded and credentials were re-encrypted for this host
5. kill *only* that process group (siblings share the Wine prefix — a blanket
   `wineserver -k` would take them down), `shred` the config
6. restart the unit, which now auto-logins from the saved file

Step 5 is the subtle one: because demo farms commonly share a single Wine prefix,
the kill must be scoped to the bootstrap's process group.

## Testing strategy

The network-free surface — config validation, command construction, output
parsing, credential resolution, model invariants — is covered by unit tests. The
SSH-executing paths are validated against real hosts; their orchestration is kept
thin precisely so the untested portion is minimal glue.
