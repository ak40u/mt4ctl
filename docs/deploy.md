# Deploy

`mt4_deploy` is *kubectl-apply for one MT4 terminal*: it reconciles a terminal's
strategy files to a local **bundle**, idempotently, touching only what mt4ctl
manages. It is **apply-only** — selection, lot sizing, magic numbers, chart
generation, and compilation all stay with the caller.

## The bundle

A bundle is a local directory mirroring the MT4 data layout:

```
<bundle>/
  profiles/default/<name>.chr        # ready charts (one expert each)
  MQL4/Experts/<folder>/<ea>.ex4     # the compiled experts those charts reference
```

`bundle` is a path **on the machine running mt4ctl** — it is read locally and
pushed over SSH, never a path on the remote host. You hand mt4ctl finished
artifacts: it does not render `.chr` or compile `.ex4`.

Each chart's `<expert> name=` value is resolved (relative to `MQL4/Experts`,
stripping an optional `Experts\` prefix, `\`→`/`) to the `.ex4` it needs; every
referenced expert must be present in the bundle or the deploy refuses with a
`BundleError` before any remote contact.

## Managed-subset reconcile

mt4ctl tracks exactly what **it** deployed, in a private manifest at
`<data_dir>/.mt4ctl/deployed.json`. The diff is computed against that manifest,
not against the whole terminal, so anything mt4ctl did not place is **foreign**
and is left strictly untouched:

- A watchdog's own `profiles/default/*.chr` (and the `.ex4` it references) survive
  every deploy — they never appear in the manifest, so they are never removed.
- A bundle file that would land on an existing **unmanaged** file is a conflict:
  the deploy **refuses** rather than silently overwriting it. Remove or rename it
  on the host, or drop it from the bundle.

**Managed scope is `profiles/default` only.** Charts in other profiles are out of
scope and not scanned. An `.ex4` is removed only when **no** surviving chart in
`profiles/default` (managed *or* foreign) still references it; if a reference is
ambiguous or unparsable, the `.ex4` is kept (fail-safe) and the situation is noted.

The on-disk hashes are authoritative over the manifest: if a managed file was
changed out from under mt4ctl (drift), the bundle version wins and the drift is
reported.

## Write order: stop → drain → backup → apply → start

MetaTrader **rewrites `profiles/default/*.chr` when it exits**. Writing charts
under a running (or still-shutting-down) terminal would have the write clobbered
on exit. So a real deploy:

1. **stop** the systemd unit;
2. **drain** — wait until `terminal.exe` has actually left the unit's cgroup
   (a stop that times out aborts the deploy *before* any write — the terminal is
   then restarted, never left mid-applied);
3. **recompute** the `.chr` diff against the now-quiesced files (the pre-stop
   classification is stale because MT4 just rewrote them on exit; `.ex4` is never
   written by MT4, so its pre-stop diff stands);
4. **backup** the current managed files **and the manifest** to
   `<data_dir>/.mt4ctl/backups/<ts>.tar` (newest 3 kept);
5. **apply** — upload the changed subset to a per-invocation staging dir, verify
   each staged file's sha256, `mv` it into place atomically, remove dropped files,
   then write `deployed.json` **last**, from the on-disk hashes (so the manifest
   can never claim a file that was not faithfully written);
6. **start** the unit again — always, in a `finally:`, even if apply failed.

A re-run with no changes skips stop/apply entirely but **still runs verify** — so
"no changes" can never hide a terminal that is silently down.

### File ownership

Only `systemctl` (stop/start) runs as root. **File mutations run as the unit's
own `User=`** (the SSH user is that user on a correctly-configured host, matching
`mt4_login`), and everything written is `chown`ed to it. This is required: files
owned by root would block MetaTrader — running as that user — from rewriting
`.chr`/`deployed.json` on its next exit.

## Concurrency

A per-terminal lockdir, `<data_dir>/.mt4ctl/deploy.lock.d`, is acquired right
after the local bundle read and held across the **whole** sequence (state read
and diff included), then released in a nested `finally:` so a raising `start` can
never strand it.

- **deploy ↔ deploy is serialized**: a second deploy fails fast while one is
  running (a stale lock older than ~10 min can be taken over).
- **deploy ↔ external watchdog is advisory.** mt4ctl cannot force an external
  process to cooperate. If you run a watchdog that restarts terminals, have it
  take the same lock around its restart:

  ```bash
  LOCK="$DATA_DIR/.mt4ctl/deploy.lock.d"
  if mkdir "$LOCK" 2>/dev/null; then
    trap 'rmdir "$LOCK"' EXIT
    systemctl restart "$UNIT"   # your watchdog's action, now deploy-safe
  else
    echo "deploy in progress — skipping this cycle"
  fi
  ```

## Verify is report-only

After restart, verify reports health — it never reverts a deploy:

- the systemd service is `active`;
- the broker connection is up (`None` = **inconclusive**, not a failure — socket
  attribution needs same-user or root; the live-trading bit is advisory only);
- each expected expert has a fresh **load line in the terminal log**, read from a
  cursor captured just before restart (rotation- and truncation-safe), **not**
  inferred from the just-written `.chr` (which would be circular).

A failed verify (`verify_ok=False`) means *check the terminal*, not *the deploy
broke* — the files were placed and the unit restarted regardless. Recovery is
always to re-deploy a known-good bundle.

## Recovery

There is **no `mt4_rollback` command** by design. A pre-apply backup is kept and
is restored **internally** if an apply fails (touched paths are purged, then the
backup is re-extracted, before the terminal restarts — so it never starts on a
half-applied tree). For operator-level recovery, **re-deploy the previous
bundle** — the caller holds it.

## Trust boundary

mt4ctl deploys whatever `.ex4`/`.chr` it is handed, including onto a **live**
account (which additionally requires `confirm=true`). It does not sign, scan, or
verify the *provenance* of bundle contents. **The operator owns bundle integrity
end-to-end.** Build-compatibility checking (does this `.ex4` match the terminal's
MT4 build?) is a documented **non-goal** in this version — `.ex4` portability is
your responsibility; a load failure will surface in the verify report.

## Caveats

- **Same filesystem.** Staging lives under `<data_dir>/.mt4ctl` and files are
  `mv`'d into `profiles/default` / `MQL4/Experts`; atomic `mv` requires one
  filesystem. The deploy preflight **fails** if `.mt4ctl` and the chart tree are
  on different devices (e.g. a WSL **DrvFs** mount split). Keep the whole data dir
  on one filesystem (the Linux/WSL ext4 side, not `/mnt/c`).
- **Shared Wine prefix.** Demo farms often share one Wine prefix across terminals.
  Deploy only stops/starts the *one* unit and only touches *its* data dir, but be
  aware that a prefix-wide action (unrelated to deploy) affects siblings.
- **Deploy-only host tools.** Deploy needs GNU `tar` and a sha256 tool
  (`sha256sum`/`shasum`/`openssl`) on the host; these are checked at the deploy
  preflight (a deploy-time failure), **not** by `mt4_doctor` — so lifecycle-only
  users are never told they are missing a tool they do not need.

## Non-goals (apply-only)

Selection · lot sizing · magic numbers · capital distribution · `.chr` generation
· `.ex4` compilation · signing · build-compatibility enforcement — all belong to
the caller that builds the bundle.
