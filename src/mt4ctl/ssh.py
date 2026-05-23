"""Async SSH transport for running scripts on native and WSL hosts.

Remote scripts are shipped base64-encoded and decoded on the far side
(``echo <b64> | base64 -d | bash``). This sidesteps every quoting hazard in the
``ssh -> Windows cmd.exe -> wsl.exe -> bash`` chain: the only character classes
that ever cross a shell boundary are the base64 alphabet plus pipes, all of
which survive ``cmd.exe`` and ``bash`` unharmed.

Nothing here interprets command output — callers own parsing.
"""

from __future__ import annotations

import asyncio
import base64
import shlex
from dataclasses import dataclass

from .errors import RemoteCommandError
from .models import Host, HostKind

# Connection reuse + fail-fast timeouts keep multi-host fan-out snappy and avoid
# hanging on an unreachable box.
SSH_BASE_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=3",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a remote command."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _wrap_for_host(host: Host, inner: str, *, root: bool) -> str:
    """Wrap a ``bash -c`` payload for the host's execution model."""
    if host.kind is HostKind.WSL:
        user = "-u root " if root else ""
        # The double-quoted payload is opaque to cmd.exe and forwarded to wsl.
        return f'wsl -d {host.wsl_distro} {user}-- bash -c "{inner}"'
    if root:
        return f'sudo bash -c "{inner}"'
    return f'bash -c "{inner}"'


def build_argv(host: Host, script: str, *, root: bool = False) -> list[str]:
    """Construct the full ``ssh`` argv that runs *script* on *host*.

    Exposed (and pure) so it can be unit-tested without a network.
    """
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    # pipefail makes the pipeline fail closed: if base64 is missing or the
    # decode fails, the command exits non-zero instead of running an empty bash.
    inner = f"set -o pipefail; echo {payload} | base64 -d | bash"
    remote = _wrap_for_host(host, inner, root=root)
    return ["ssh", *SSH_BASE_OPTS, host.ssh, remote]


async def run(
    host: Host,
    script: str,
    *,
    root: bool = False,
    timeout: float = 60.0,
    check: bool = True,
) -> CommandResult:
    """Run *script* on *host* and return its result.

    Args:
        host: target host.
        script: a bash snippet (multi-line allowed).
        root: obtain root (``sudo`` on native, ``wsl -u root`` on WSL).
        timeout: seconds before the SSH call is killed.
        check: raise :class:`RemoteCommandError` on non-zero exit.
    """
    argv = build_argv(host, script, root=root)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RemoteCommandError(
            host.id, 127, f"could not start ssh ({exc}); is the ssh client installed?"
        ) from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RemoteCommandError(
            host.id, 124, f"command timed out after {timeout:.0f}s"
        ) from None

    result = CommandResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )
    if check and not result.ok:
        raise RemoteCommandError(host.id, result.returncode, result.stderr)
    return result


async def fetch_bytes(host: Host, remote_path: str, *, timeout: float = 30.0) -> bytes:
    """Read a remote binary file and return its raw bytes.

    Implemented via base64 over stdout so it works through the same WSL chain.
    """
    script = f"base64 {shlex.quote(remote_path)}"
    result = await run(host, script, timeout=timeout)
    return base64.b64decode(result.stdout)


def build_put_argv(host: Host, dest_dir: str) -> list[str]:
    """Construct the ``ssh`` argv that extracts a stdin-fed tar into *dest_dir*.

    Unlike :func:`build_argv` — which pipes the control script *through* stdin
    (``echo <b64> | base64 -d | bash``) and so consumes fd 0 — this reserves
    stdin for the raw tar stream. The extraction script is base64-encoded (with
    *dest_dir* already ``shlex.quote``-d inside it, so nothing but the base64
    alphabet crosses the ``ssh → cmd.exe → wsl → bash`` chain) and decoded into a
    **process-substitution file** (``bash <(echo <b64> | base64 -d)``): bash reads
    the script from that fd while its real stdin — the tar — flows to ``tar -x``.

    Extraction passes ``--no-same-owner`` (extract as the invoking user, never the
    archived uid). Absolute/``..`` paths are guarded three ways: GNU tar's default
    strips leading ``/`` and refuses members escaping the extraction dir, the tar
    is built only from members that passed :func:`mt4ctl.deploy.validate_member`,
    and ``-C`` confines extraction. (GNU tar has no ``--no-absolute-names``; ``-P``
    is its *opposite* — disabling the strip — so it is deliberately not passed.)

    Exposed and pure so it can be unit-tested without a network.
    """
    put_script = (
        "set -o pipefail\n"
        f"mkdir -p {shlex.quote(dest_dir)}\n"
        f"tar --no-same-owner -x -C {shlex.quote(dest_dir)}\n"
    )
    payload = base64.b64encode(put_script.encode("utf-8")).decode("ascii")
    inner = f"bash <(echo {payload} | base64 -d)"
    remote = _wrap_for_host(host, inner, root=False)
    return ["ssh", *SSH_BASE_OPTS, host.ssh, remote]


async def put_tar(host: Host, tar_bytes: bytes, dest_dir: str, *, timeout: float = 120.0) -> None:
    """Upload *tar_bytes* and extract it into *dest_dir* on *host* (created if absent).

    The raw tar is streamed to the remote ``tar -x`` over stdin (see
    :func:`build_put_argv`). Raises :class:`RemoteCommandError` on a non-zero exit
    (missing/Busybox ``tar``, decode failure) or a timeout, with actionable stderr.

    Spike result (recorded): the stdin mechanism round-tripped a binary tar
    byte-identical on both a real WSL host (Windows OpenSSH → cmd.exe → wsl.exe →
    Ubuntu-24.04 bash) and a native host, so it is the shipped path. If a future
    host mangles stdin, the documented fallback is chunked-append: ``mktemp`` a
    remote temp under the data dir, append base64 chunks via small ``run()`` calls
    (checking rc per chunk, truncating before the first), sha256-verify the whole
    reassembled tar against a locally-computed digest BEFORE ``tar -x``, and always
    ``rm -f`` the temp — bounded, and free of the SSH command-line size limit a
    single base64-tar-in-arg would hit on real bundles.
    """
    argv = build_put_argv(host, dest_dir)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RemoteCommandError(
            host.id, 127, f"could not start ssh ({exc}); is the ssh client installed?"
        ) from None
    try:
        _out, err = await asyncio.wait_for(proc.communicate(tar_bytes), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RemoteCommandError(
            host.id, 124, f"tar upload timed out after {timeout:.0f}s"
        ) from None
    rc = proc.returncode or 0
    if rc != 0:
        detail = err.decode("utf-8", "replace").strip() or "(no stderr)"
        raise RemoteCommandError(host.id, rc, f"tar upload failed: {detail}")
