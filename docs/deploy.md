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

## Adopting an existing farm (first cutover)

On a terminal whose strategies were **not** placed by mt4ctl there is no manifest,
so the very first `mt4_deploy` refuses every existing file as an unmanaged
overwrite. To take that farm under management, run **`mt4_adopt`** once first:

```bash
mt4ctl adopt demo3 ./current-bundle     # record what the terminal already runs
mt4ctl deploy demo3 ./current-bundle --dry-run   # should now report "no changes"
mt4ctl deploy demo3 ./next-bundle               # reconcile forward from here on
```

`adopt` records the bundle's footprint into `deployed.json` at the files' **current
on-disk hashes** — it is *records-only*: no strategy file is touched, the unit is
never stopped or restarted. It is **bundle-scoped**: only the bundle's own paths
are recorded, so foreign files (a watchdog's chart) stay foreign and are never
adopted. Every bundle file must already be present on the host — the premise is
that the farm runs this bundle; a missing file is refused (no partial manifest).
A self-contained bundle is required (every chart's `.ex4` present), same as deploy.
A live terminal needs `confirm=true`.

For transparency, adopt also **lists any live charts on the host that are not in the
bundle** — so you can confirm at a glance what it left foreign (a watchdog's chart,
say) rather than having to trust that it did. It reports them; it does not touch them.

> **`.chr` caveat.** `.ex4` files are never rewritten by MT4, so adopt records them
> exactly and a later `--dry-run` stays clean. MT4 *does* rewrite `.chr` on exit,
> so a `.chr` adopted from a live terminal may show as `update` on a deploy taken
> **after** the terminal next restarts — a benign re-place of the canonical chart
> (the same post-drain `.chr` reconcile a normal deploy does). It is not data loss.

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
"no changes" can never hide a terminal that is silently down. (`--reset-market-watch`
forces the stop/start cycle even with no changes — see below.)

### File ownership

Only `systemctl` (stop/start) runs as root. **File mutations run as the unit's
own `User=`** (the SSH user is that user on a correctly-configured host, matching
`mt4_login`), and everything written is `chown`ed to it. This is required for two
reasons: MetaTrader — running as that user — rewrites its `.chr` files on exit, so
a root-owned `.chr` would block it; and `deployed.json` (mt4ctl's own manifest,
which MetaTrader never touches) must stay writable by the unit user so the *next*
mt4ctl run can rewrite it.

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

After a restart, verify **polls** the terminal until it is healthy or a timeout
elapses (default ~120 s, `--verify-timeout`), then reports its state — it never
reverts a deploy. Polling matters because a real terminal needs ~30–90 s to
reconnect to the broker and a minute to load a large EA set; a single immediate
snapshot would report essentially every healthy deploy as "not confirmed". A
no-change health confirmation (no restart) takes one snapshot, not the poll loop.

Verify checks:

- the systemd service is `active`;
- the broker connection is up (`None` = **inconclusive**, not a failure — socket
  attribution needs same-user or root; the live-trading bit is advisory only);
- each expected expert has a fresh **load line in the terminal log**, read from a
  cursor captured just before restart (rotation- and truncation-safe), **not**
  inferred from the just-written `.chr` (which would be circular). Progress is
  summarized by count (`N/total loaded, M pending`); names are listed only for
  experts that **errored**.

A failed verify (`verify_ok=False`) means *check the terminal*, not *the deploy
broke* — the files were placed and the unit restarted regardless. Recovery is
always to re-deploy a known-good bundle.

The same poll routine is available standalone as `mt4_verify <terminal>` /
`mt4ctl verify <terminal>` for use after any restart, not just a deploy.

## Recovery

There is **no `mt4_rollback` command** by design. A pre-apply backup is kept and
is restored **internally** if an apply fails (touched paths are purged, then the
backup is re-extracted, before the terminal restarts — so it never starts on a
half-applied tree). For operator-level recovery, **re-deploy the previous
bundle** — the caller holds it.

## Market Watch reset (optional)

`deploy --reset-market-watch` (tool arg `reset_market_watch`) caps unbounded
Market Watch growth. MT4 never prunes a symbol once it has been selected, so a
terminal's `symbols.sel` accumulates symbols across rotations (sockets it does not
need). With the flag set, the deploy — inside its **stopped window**, after drain
and before start — backs up then deletes `history/*/symbols.sel`; on the next
start MT4 rebuilds Market Watch from scratch as **the broker default set (~10
symbols) plus every loaded chart's symbol**. So every traded symbol (and any
no-expert *conversion* chart's symbol) is preserved, while the carry-over is gone.

- It is a **file delete in a stopped window**, never a binary write — mt4ctl does
  not author the undocumented `symbols.sel` format. The removed file is copied to
  `<data_dir>/.mt4ctl/backups/` first.
- The flag forces a stop/start cycle **even with no file changes** (the reset
  needs the terminal down), so it can be used on its own to reset Market Watch.
- It only deletes while the terminal is confirmed stopped (a running terminal
  rewrites `symbols.sel` from its in-memory Market Watch on exit).
- On a `--dry-run`, it is reported but not performed.
- A live terminal still requires `confirm=true` (the deploy's own live gate).

To *add* a needed symbol to Market Watch, put a chart for it in the bundle (MT4
adds a chart's symbol on load) — that is the bundle builder's job, kept out of
generic mt4ctl. The ~10 broker defaults always return on a rebuild; removing them
would require writing `symbols.sel`, which is out of scope.

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
