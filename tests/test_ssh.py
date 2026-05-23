"""SSH command construction (pure, no network)."""

from __future__ import annotations

import base64

import pytest

from mt4ctl import ssh
from mt4ctl.errors import RemoteCommandError
from mt4ctl.models import Host, HostKind
from mt4ctl.ssh import CommandResult, build_argv


def _payload(argv: list[str]) -> str:
    """Decode the base64 blob embedded in the remote command."""
    remote = argv[-1]
    token = remote.split("echo ", 1)[1].split(" |", 1)[0]
    return base64.b64decode(token).decode()


def test_native_argv_roundtrips_script():
    host = Host(id="vps", ssh="my-vps", kind=HostKind.NATIVE)
    argv = build_argv(host, "echo hi", root=False)
    assert argv[0] == "ssh"
    assert argv[-2] == "my-vps"
    assert argv[-1].startswith("bash -c ")
    assert _payload(argv) == "echo hi"


def test_native_root_uses_sudo():
    host = Host(id="vps", ssh="my-vps", kind=HostKind.NATIVE)
    argv = build_argv(host, "systemctl restart x", root=True)
    assert argv[-1].startswith("sudo bash -c ")


def test_wsl_argv_wraps_in_wsl():
    host = Host(id="box", ssh="box", kind=HostKind.WSL, wsl_distro="Ubuntu-24.04")
    argv = build_argv(host, "uname -a")
    assert "wsl -d Ubuntu-24.04 --" in argv[-1]
    assert "-u root" not in argv[-1]
    assert _payload(argv) == "uname -a"


def test_wsl_root_adds_user_root():
    host = Host(id="box", ssh="box", kind=HostKind.WSL, wsl_distro="Ubuntu-24.04")
    argv = build_argv(host, "systemctl stop x", root=True)
    assert "wsl -d Ubuntu-24.04 -u root --" in argv[-1]


def test_multiline_and_special_chars_survive_encoding():
    host = Host(id="box", ssh="box", kind=HostKind.WSL, wsl_distro="Ubuntu-24.04")
    script = 'echo "a|b"\nfor x in 1 2; do echo $x; done'
    argv = build_argv(host, script)
    assert _payload(argv) == script


def test_build_argv_fails_closed_with_pipefail():
    argv = build_argv(Host(id="h", ssh="h"), "echo hi")
    assert "set -o pipefail" in argv[-1]


async def test_run_normalizes_local_spawn_failure(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    with pytest.raises(RemoteCommandError, match="could not start ssh"):
        await ssh.run(Host(id="h", ssh="h"), "echo hi")


async def test_fetch_bytes_quotes_remote_path(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_run(host, script, **kw):
        captured["script"] = script
        return CommandResult(0, "", "")

    monkeypatch.setattr(ssh, "run", fake_run)
    await ssh.fetch_bytes(Host(id="h", ssh="h"), "/tmp/x$(touch pwned).png")
    # the dangerous path must be single-quoted, not left to expand
    assert "'/tmp/x$(touch pwned).png'" in captured["script"]
