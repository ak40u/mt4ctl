# Architecture

`mt4ctl` is a thin MCP adapter over a typed, network-free core. Each module has a
single responsibility and depends only on those below it.

```
cli.py           console entry point — serve (default) / list / doctor / init
   │
server.py        MCP tool definitions (FastMCP) — one thin wrapper per tool
   │
   ├── operations.py   status · logs · screenshot · control · experts · info · deploy · adopt
   ├── login.py        headless first-login bootstrap
   ├── diagnostics.py  doctor checks (registry, SSH, tools, units, connections)
   ├── deploy.py       pure deploy core — bundle read, member allowlist, reconcile diff
   │
   ├── scripts.py      pure bash builders (no I/O) ── the only place shell lives
   ├── ssh.py          async SSH transport (base64-framed) + put_tar upload, native + WSL
   ├── auth.py         credential resolution chain
   ├── config.py       YAML → Registry, with validation
   ├── models.py       Host · Terminal · Registry · TerminalStatus · Expert … (dataclasses)
   └── errors.py       typed, actionable exceptions
```

`cli.py` is the installed `mt4ctl` command: with no subcommand it runs the MCP
stdio server (`server.serve()`); `list`/`doctor`/`init` are setup helpers that
reuse the same core without an MCP client.

## Design choices

**Pure shell builders.** Every remote command is produced by a pure function in
`scripts.py` (or `login.py`) and executed by `ssh.py`. Because the builders are
side-effect-free, the gnarly shell logic is unit-tested directly, and the
orchestration layer never concatenates commands by hand.

**Base64 framing over stdin.** Remote scripts are base64-encoded and streamed over
the ssh process's **stdin** into a fixed-size remote command (`base64 -d | bash`).
Only the base64 alphabet ever crosses the `ssh → cmd.exe → wsl.exe → bash`
boundary, eliminating an entire class of quoting bugs; and because the script
rides on stdin rather than the command line, the command stays a constant ~40
bytes no matter how large the generated script is — so a deploy/adopt over a big
bundle never hits the Windows `cmd.exe` ~8 KB command-line limit. The same stdin
channel carries the binary tar upload (`put_tar`); binary files (screenshots) come
back over stdout.

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

1. stop the unit (free the terminal slot); abort if the stop fails
2. create a transient `[Common]` startup config with `mktemp` (fresh, mode 600)
   and write `login`/`password`/`server` into a **single-quoted heredoc** so the
   shell never expands the values
3. stamp a marker file, then launch the terminal **in its own process group**
   (`setsid`), reusing the unit's `WorkingDirectory` / `WINEPREFIX` / `DISPLAY`
4. wait until `config/accounts.ini` is *newer than the marker* — MetaTrader's
   signal that authentication succeeded and credentials were re-encrypted here
5. a `trap cleanup EXIT HUP INT TERM` **always** kills *only* that process group
   (siblings share the Wine prefix — a blanket `wineserver -k` would take them
   down) and `shred`s the config, even if an earlier step fails
6. restart the unit, which now auto-logins from the saved file

Two subtleties: the kill is scoped to the bootstrap's process group because demo
farms commonly share a single Wine prefix; and the cleanup is a trap, not a
trailing statement, so the credential file is removed on any exit path. The
one-shot runs as the SSH user, so that user must be the unit's `User=`.

## Testing strategy

The network-free surface — config validation, command construction, output
parsing, credential resolution, model invariants — is covered by unit tests. The
SSH-executing paths are validated against real hosts; their orchestration is kept
thin precisely so the untested portion is minimal glue.
