"""Console entry point: a thin human-facing CLI around the MCP server.

``mt4ctl`` with no subcommand runs the MCP stdio server (the form MCP clients
launch). The ``list``/``doctor``/``init`` subcommands exist purely to make setup
debuggable without an MCP client — they do not duplicate the MCP tool surface.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__, diagnostics
from .config import load_registry, resolve_path
from .errors import Mt4ctlError

STARTER_REGISTRY = """\
# mt4ctl registry — fill in your real hosts and terminals. Keep this file private.
hosts:
  box:
    ssh: my-ssh-alias        # ~/.ssh/config alias or user@host
    kind: native             # 'native' (Linux) or 'wsl' (Windows + WSL2)
    # wsl_distro: Ubuntu-24.04   # required when kind: wsl
    # broker_host: demo.broker.example.com   # optional, sharpens connection checks

terminals:
  t1:
    host: box
    service: mt4-t1          # systemd unit name
    data_dir: /home/trader/mt4/t1
    display: ":99"
    account: "1000001"
    env: demo                # 'demo' or 'live' (live gates mutations)
"""


def _print_err(message: str) -> None:
    print(message, file=sys.stderr)


def _cmd_serve() -> int:
    """Run the MCP stdio server, or explain itself if launched interactively."""
    if sys.stdin.isatty():
        _print_err(
            "mt4ctl is an MCP stdio server — launch it from an MCP client, not directly.\n"
            "  • Claude Code:  claude mcp add --scope user mt4ctl -- uvx mt4ctl\n"
            "  • Diagnose:     mt4ctl doctor\n"
            "  • Details:      https://github.com/ak40u/mt4ctl#connect-to-an-mcp-client\n"
            "Run `mt4ctl --help` for options."
        )
        return 2
    from .server import serve

    serve()
    return 0


def _cmd_list() -> int:
    try:
        registry = load_registry()
    except Mt4ctlError as exc:
        _print_err(f"error: {exc}")
        return 1
    print(f"registry: {resolve_path()}")
    print(f"hosts: {', '.join(registry.hosts) or '(none)'}\n")
    for t in registry.terminals.values():
        print(
            f"  {t.id:<14} host={t.host:<12} account={t.account or '-':<12} env={t.env.value}"
        )
    return 0


def _cmd_doctor() -> int:
    try:
        registry = load_registry()
    except Mt4ctlError as exc:
        _print_err(f"error: {exc}")
        return 1
    print(f"registry: {resolve_path()}\n")
    checks = asyncio.run(diagnostics.run_diagnostics(registry))
    print(diagnostics.format_checks(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def _cmd_init(path: str | None) -> int:
    target = (
        Path(path).expanduser()
        if path
        else Path.home() / ".config" / "mt4ctl" / "terminals.yaml"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic create (O_EXCL) at mode 600: the registry becomes private host/account
    # inventory, and this also closes the exists-then-write race.
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        _print_err(f"error: {target} already exists; refusing to overwrite.")
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(STARTER_REGISTRY)
    print(
        f"wrote starter registry to {target}\n"
        "Edit it, then verify with:  mt4ctl list   (and:  mt4ctl doctor)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mt4ctl",
        description=(
            "Manage headless MetaTrader 4 terminals over SSH. With no subcommand, "
            "runs the MCP stdio server for an MCP client (Claude Code / Desktop). "
            "The subcommands help you set up and debug without a client."
        ),
        epilog="Config: MT4CTL_CONFIG or ~/.config/mt4ctl/terminals.yaml. "
        "Docs: https://github.com/ak40u/mt4ctl",
    )
    parser.add_argument("--version", action="version", version=f"mt4ctl {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP stdio server (default)")
    sub.add_parser("list", help="list configured terminals (offline)")
    sub.add_parser("doctor", help="check registry, SSH, remote tools, units")
    p_init = sub.add_parser("init", help="write a starter terminals.yaml")
    p_init.add_argument("path", nargs="?", help="where to write (default: XDG config path)")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = args.command or "serve"
    if command == "serve":
        raise SystemExit(_cmd_serve())
    if command == "list":
        raise SystemExit(_cmd_list())
    if command == "doctor":
        raise SystemExit(_cmd_doctor())
    if command == "init":
        raise SystemExit(_cmd_init(args.path))
    raise SystemExit(2)  # unreachable; argparse rejects unknown commands


if __name__ == "__main__":
    main()
