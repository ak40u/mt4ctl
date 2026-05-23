"""SSH command construction (pure, no network)."""

from __future__ import annotations

import base64

import pytest

from mt4ctl import ssh
from mt4ctl.errors import RemoteCommandError
from mt4ctl.models import Host, HostKind
from mt4ctl.ssh import CommandResult, build_argv, build_put_argv


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


# --------------------------------------------------------------------------- #
# put_tar upload transport (Phase 3)
# --------------------------------------------------------------------------- #
def test_put_argv_reserves_stdin_via_process_substitution():
    host = Host(id="vps", ssh="my-vps", kind=HostKind.NATIVE)
    argv = build_put_argv(host, "/home/trader/d/.mt4ctl/staging/u1")
    remote = argv[-1]
    # process-substitution decode keeps fd 0 free for the tar (NOT `| bash`)
    assert "bash <(echo " in remote
    assert "| base64 -d)" in remote
    assert "| bash" not in remote  # must NOT use run()'s stdin-script pipeline
    # the extraction script (base64) carries the hardened tar flag + dest
    script = _payload(argv)
    assert "tar --no-same-owner -x -C " in script
    assert "--no-absolute-names" not in script  # not a GNU tar option; default guards traversal
    assert "mkdir -p " in script


def test_put_argv_passes_dest_only_as_base64_token():
    host = Host(id="vps", ssh="my-vps", kind=HostKind.NATIVE)
    dest = "/home/trader/Live Terminal 2000001/.mt4ctl/staging/u1"
    argv = build_put_argv(host, dest)
    # the raw dest (with its space + injection bait) never appears on the wire
    assert dest not in argv[-1]
    evil = "/tmp/$(touch pwned)/.mt4ctl/staging/u1"
    argv2 = build_put_argv(host, evil)
    assert "$(touch pwned)" not in argv2[-1]
    # ...only inside the base64'd script, single-quoted so it cannot expand
    assert "'/tmp/$(touch pwned)/.mt4ctl/staging/u1'" in _payload(argv2)


def test_put_argv_wsl_wrapper():
    host = Host(id="box", ssh="box", kind=HostKind.WSL, wsl_distro="Ubuntu-24.04")
    argv = build_put_argv(host, "/d/.mt4ctl/staging/u1")
    assert "wsl -d Ubuntu-24.04 -- bash -c " in argv[-1]
    assert "-u root" not in argv[-1]  # file writes are never root


def test_put_argv_native_wrapper():
    host = Host(id="vps", ssh="vps", kind=HostKind.NATIVE)
    argv = build_put_argv(host, "/d/s")
    assert argv[0] == "ssh"
    assert argv[-2] == "vps"
    assert argv[-1].startswith("bash -c ")


async def test_put_tar_raises_on_nonzero_exit(monkeypatch):
    class FakeProc:
        returncode = 2

        async def communicate(self, data=None):
            return (b"", b"tar: short read")

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(RemoteCommandError, match="tar upload failed"):
        await ssh.put_tar(Host(id="h", ssh="h"), b"tarbytes", "/d/s")


async def test_put_tar_succeeds_on_zero_exit(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            captured["data"] = data
            return (b"", b"")

    async def fake_exec(*a, **k):
        captured["argv"] = a
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    await ssh.put_tar(Host(id="h", ssh="h"), b"RAWTAR", "/d/s")
    assert captured["data"] == b"RAWTAR"  # raw tar fed to stdin
    assert captured["argv"][0] == "ssh"


async def test_put_tar_normalizes_local_spawn_failure(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    with pytest.raises(RemoteCommandError, match="could not start ssh"):
        await ssh.put_tar(Host(id="h", ssh="h"), b"x", "/d/s")
