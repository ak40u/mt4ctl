# Configuration

`mt4ctl` is driven by a single YAML registry that describes your **hosts** and the
**terminals** running on them. No infrastructure details live in the source tree.

## Where the registry is loaded from

If `MT4CTL_CONFIG` is set, it **must** point to an existing file — `mt4ctl` fails
fast rather than falling back, so a typo doesn't silently load the wrong registry.
Otherwise it searches, first existing wins:

1. `$XDG_CONFIG_HOME/mt4ctl/terminals.yaml` (defaults to `~/.config/mt4ctl/terminals.yaml`)
2. `./terminals.yaml` in the current working directory

Start from [`../examples/terminals.example.yaml`](../examples/terminals.example.yaml)
or run `mt4ctl init`.

## Hosts

```yaml
hosts:
  <host-id>:
    ssh: <alias-or-user@host>   # required — passed straight to `ssh`
    kind: native | wsl          # default: native
    wsl_distro: Ubuntu-24.04    # required when kind: wsl
    broker_host: <hostname>     # optional — sharpens connection detection
```

| Field | Required | Notes |
| --- | :---: | --- |
| `ssh` | ✓ | An `~/.ssh/config` alias is recommended (handles user, key, `IdentitiesOnly`). |
| `kind` | – | `native` → root via `sudo`; `wsl` → commands wrapped in `wsl -d <distro> --`, root via `wsl -u root`. |
| `wsl_distro` | for `wsl` | The distro name shown by `wsl -l`. |
| `broker_host` | – | When set, only sockets to this host's resolved IPs count as a broker connection. |

## Terminals

```yaml
terminals:
  <terminal-id>:
    host: <host-id>             # required — must exist under hosts:
    service: <systemd-unit>     # required — e.g. mt4-demo1
    data_dir: <path>            # required — parent of logs/ and config/
    display: ":99"              # X display for screenshots (default ":0")
    account: "<login>"          # login number; also used to find the window
    env: demo | live            # default: demo; live gates mutations
    window_match: "<string>"    # optional override for window search
```

`data_dir` may contain spaces (e.g. `"Live Terminal 2000001"`); it is
quoted safely on the remote side.

## Credentials (for `mt4_login`)

Passwords are resolved on demand, never stored by `mt4ctl`:

1. the `password` argument to `mt4_login`
2. the `MT4CTL_PASSWORD_<account>` environment variable
3. the `<account>` key in a JSON secrets file at `MT4CTL_CREDENTIALS`
   (or `~/.config/mt4ctl/credentials.json`)

```json
{
  "1000001": "your-account-password"
}
```

Keep this file out of version control (the default `.gitignore` already excludes
`credentials.json`).

## Remote host requirements

Each managed host needs: `bash`, `systemctl`, `ss`, `getent`, `base64`, `stat`,
and — for `mt4_screenshot` — an X server on the configured display plus
`imagemagick` (`import`) or `scrot`, and `xdotool`.

Per-terminal connection detection reads **cgroup v2**
(`/sys/fs/cgroup/<unit>/cgroup.procs`), the default on modern systemd. On cgroup
v1 hosts the connection column degrades to `?`; service state and logs are
unaffected.

For connection attribution to be accurate, the SSH user must be able to see the
terminal processes' sockets — i.e. the **SSH user is the unit's `User=`**, or you
connect as root. When `ss` cannot expose process metadata, or when a configured
`broker_host` fails to resolve, the connection column reports `?` rather than
guessing.

`mt4_login` also runs its one-shot under the **SSH account**, reusing the unit's
`WorkingDirectory`, `WINEPREFIX`, and `DISPLAY` (read from the unit's inline
`Environment=`; `EnvironmentFile=` is not resolved). Use it on units whose
`User=` matches your SSH user (the common single-user farm setup); for units that
run as a different user, log in via that user's session instead.
