"""Pure, network-free core of the deploy capability.

Three responsibilities, all side-effect-free and unit-testable without a host:

* :func:`read_bundle` — read a local bundle dir (mirroring the MT4 layout) into
  ``(files, chart_to_ex4)`` dicts: a sha256 per managed file plus each chart's
  referenced expert, with an integrity guard and the member-safety allowlist.
* :func:`compute_plan` — diff ``(bundle, remote_state)`` into a
  :class:`~mt4ctl.models.DeployPlan` under managed-subset semantics: mt4ctl only
  reconciles what it deployed (tracked in ``deployed.json``) and never touches
  foreign files.
* :func:`validate_member` / :data:`MEMBER_RE` — the *single* path allowlist the
  whole pipeline reuses (the tar builder and the remote apply import it so they
  cannot diverge).

This module owns the canonical path identity: a POSIX-relative path under the
bundle root (``profiles/default/<x>.chr`` or ``MQL4/Experts/<...>.ex4``). Phases
that build the upload tar, the apply script, and the manifest all key on it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BundleError, DeployError
from .models import DeployPlan

# Managed-member allowlist (single source of truth). A managed path is either a
# chart in the default profile or an expert binary anywhere under MQL4/Experts.
# The ``..`` / absolute / symlink guards live in validate_member + read_bundle,
# because ``.+`` here would otherwise match a traversal component.
MEMBER_RE = re.compile(r"^(profiles/default/[^/]+\.chr|MQL4/Experts/.+\.ex4)$")

# Bumped if the on-disk deployed.json shape ever changes; parse_manifest rejects
# anything without it rather than coercing a corrupt file to an empty manifest.
MANIFEST_VERSION = 1

_EX4_ROOT = "MQL4/Experts"
_CHART_DIR = "profiles/default"


@dataclass(frozen=True, slots=True)
class RemoteState:
    """All inputs the diff needs, as plain data (no SSH).

    Attributes:
        deployed: the parsed ``deployed.json`` manifest (relpath -> sha256), or
            ``None`` when the manifest is absent (first deploy).
        remote_hashes: actual sha256 of each currently-present managed file; the
            authority over the manifest when the two disagree (drift).
        remote_chart_refs: every live ``profiles/default/*.chr`` (relpath) mapped
            to its referenced ``.ex4`` relpath, or ``None`` when the chart's EA
            reference is unparsable/ambiguous (drives foreign-``.ex4`` retention).
        dest_stats: per bundle destination relpath, whether it already exists on
            the host (drives the unmanaged-overwrite refusal).
    """

    deployed: dict[str, str] | None = None
    remote_hashes: dict[str, str] = field(default_factory=dict)
    remote_chart_refs: dict[str, str | None] = field(default_factory=dict)
    dest_stats: dict[str, bool] = field(default_factory=dict)


def validate_member(name: str) -> None:
    """Raise :class:`BundleError` unless *name* is a safe, in-scope member path.

    Rejects absolute paths, any ``..`` component, and anything outside the
    managed allowlist. Imported by the tar builder and the apply path so a
    crafted member is refused everywhere identically.
    """
    if not name or name.startswith("/"):
        raise BundleError(f"unsafe member (absolute or empty path): {name!r}")
    if ".." in name.split("/"):
        raise BundleError(f"unsafe member (parent-directory reference): {name!r}")
    if not MEMBER_RE.match(name):
        raise BundleError(
            f"member outside managed scope ({_CHART_DIR}/*.chr or {_EX4_ROOT}/**/*.ex4): "
            f"{name!r}"
        )


def normalize_ea_ref(name: str) -> str:
    """Normalize a ``.chr`` ``<expert> name=`` value to a POSIX path under MQL4/Experts.

    The stored value is relative to ``MQL4/Experts`` (e.g. ``<folder>\\<ea>``); an
    ``Experts\\`` prefix is *not* guaranteed. Strip an optional leading
    ``Experts/``, swap backslashes, and return ``<folder>/<ea>`` (no extension).
    """
    s = name.strip().replace("\\", "/").lstrip("/")
    if s.startswith("Experts/"):
        s = s[len("Experts/") :]
    return s


def ea_ref_to_ex4(name: str) -> str:
    """Map a chart's EA reference to its expected bundle ``.ex4`` relpath."""
    return f"{_EX4_ROOT}/{normalize_ea_ref(name)}.ex4"


def short_name(ex4_relpath: str) -> str:
    """The expert short-name (folder collapsed, no extension) used by verify."""
    return ex4_relpath.rsplit("/", 1)[-1].removesuffix(".ex4")


def _parse_chart_ref(text: str) -> str | None:
    """Return the ``.ex4`` relpath referenced by a ``.chr``, or ``None`` if none.

    Mirrors the remote awk parse (:func:`scripts.build_experts_script`): the first
    ``<expert>`` block's ``name=`` line wins. CRLF is tolerated.
    """
    in_expert = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "<expert>":
            in_expert = True
        elif in_expert and line.startswith("name="):
            value = line[len("name=") :].strip()
            return ea_ref_to_ex4(value) if value else None
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_bundle(bundle_dir: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read a local bundle dir into ``(files, chart_to_ex4)``.

    ``files`` maps each managed relpath to its sha256; ``chart_to_ex4`` maps each
    chart relpath to its referenced ``.ex4`` relpath. Non-managed files are
    ignored; symlinks are rejected; every chart's referenced ``.ex4`` must exist
    in the bundle (integrity guard) or :class:`BundleError` is raised.
    """
    root = Path(bundle_dir)
    if not root.is_dir():
        raise BundleError(f"bundle directory not found: {bundle_dir!r}")
    # Guard against pointing at the wrong directory: an empty/foreign tree would
    # otherwise reconcile to "remove everything managed". A real bundle has at
    # least one of the two managed roots.
    if not (root / _CHART_DIR).is_dir() and not (root / _EX4_ROOT).is_dir():
        raise BundleError(
            f"{bundle_dir!r} is not a bundle: it has neither {_CHART_DIR}/ nor {_EX4_ROOT}/"
        )

    files: dict[str, str] = {}
    chart_to_ex4: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise BundleError(f"bundle contains a symlink (refused): {rel!r}")
        if path.is_dir():
            continue
        if not MEMBER_RE.match(rel):
            continue  # stray file (README, etc.) — outside managed scope, ignored
        validate_member(rel)
        files[rel] = _sha256(path)
        if rel.endswith(".chr"):
            ref = _parse_chart_ref(path.read_text(encoding="utf-8", errors="replace"))
            if ref is not None:
                chart_to_ex4[rel] = ref

    for chart, ex4 in chart_to_ex4.items():
        if ex4 not in files:
            raise BundleError(
                f"chart {chart!r} references expert {ex4!r}, which is missing from the bundle"
            )
    return files, chart_to_ex4


def parse_manifest(raw: str | None) -> dict[str, str] | None:
    """Parse a ``deployed.json`` payload into its files map.

    ``None`` or the ``MISSING`` marker → ``None`` (first deploy). A present but
    corrupt/unparsable manifest → :class:`DeployError` (never coerced to ``{}``,
    which would silently orphan every previously-managed file).
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or text == "MISSING":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeployError(f"remote deployed.json is corrupt (invalid JSON): {exc}") from None
    if not isinstance(data, dict) or "version" not in data or "files" not in data:
        raise DeployError("remote deployed.json is corrupt (missing version/files keys)")
    if data["version"] != MANIFEST_VERSION:
        raise DeployError(
            f"remote deployed.json is version {data['version']!r}, but this mt4ctl writes "
            f"v{MANIFEST_VERSION}; upgrade mt4ctl rather than risk a mismatched reconcile"
        )
    manifest_files = data["files"]
    if not isinstance(manifest_files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in manifest_files.items()
    ):
        raise DeployError("remote deployed.json is corrupt (files is not a str->str map)")
    return manifest_files


def build_manifest(files: dict[str, str]) -> str:
    """Serialize a managed-file map to the canonical ``deployed.json`` payload.

    The exact inverse of :func:`parse_manifest` (``{"version": …, "files": …}``),
    with sorted keys for deterministic output. This is the Python-side writer used
    by ``adopt``; the deploy apply path builds the same shape on the remote from
    post-move on-disk hashes, and a parity test keeps the two in lock-step.
    """
    return json.dumps(
        {"version": MANIFEST_VERSION, "files": dict(sorted(files.items()))},
        separators=(",", ":"),
    )


def compute_plan(
    files: dict[str, str],
    chart_to_ex4: dict[str, str],
    remote_state: RemoteState,
) -> DeployPlan:
    """Diff a bundle against a terminal's managed state (managed-subset reconcile).

    Add/update/unchanged are decided against ``remote_hashes`` (authoritative over
    a stale manifest — drift is surfaced as a note). A bundle path that lands on
    an existing *unmanaged* file becomes a ``conflict`` (the deploy refuses). On
    the removal side, a managed ``.ex4`` is removed only when no surviving chart
    (bundle or foreign) references it; an ambiguous reference keeps it (fail-safe).
    """
    managed = remote_state.deployed or {}
    remote_hashes = remote_state.remote_hashes
    dest_stats = remote_state.dest_stats
    notes: list[str] = []

    add: list[str] = []
    update: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []

    for path in sorted(files):
        want = files[path]
        if path in managed:
            actual = remote_hashes.get(path)
            if actual is None:
                add.append(path)
                notes.append(f"drift: managed {path} missing on host — will re-place")
            elif actual == want:
                unchanged.append(path)
                if managed.get(path) != actual:
                    notes.append(f"drift: manifest hash for {path} stale; on-disk already matches")
            else:
                update.append(path)
                if managed.get(path) != actual:
                    notes.append(f"drift: {path} changed on host since deploy — overwriting")
        elif dest_stats.get(path, False):
            conflicts.append(path)
            notes.append(
                f"refuse: {path} exists on host but is not managed by mt4ctl — will not overwrite"
            )
        else:
            add.append(path)

    bundle_charts = {p for p in files if p.endswith(".chr")}
    managed_charts = {p for p in managed if p.endswith(".chr")}

    # Charts present after the deploy reference their .ex4: bundle charts (via
    # chart_to_ex4) and preserved foreign charts. Managed charts absent from the
    # bundle are being removed, so their references do not survive.
    references_after: set[str] = set(chart_to_ex4.values())
    ambiguous = False
    for chart, ref in remote_state.remote_chart_refs.items():
        if chart in bundle_charts or chart in managed_charts:
            continue
        if ref is None:
            ambiguous = True
            notes.append(
                f"ambiguous: foreign chart {chart} has an unparsable EA reference — "
                ".ex4 removal kept conservative"
            )
        else:
            references_after.add(ref)

    remove: list[str] = []
    foreign: list[str] = []
    for path in sorted(p for p in managed if p not in files):
        if path.endswith(".chr"):
            remove.append(path)
        elif path in references_after:
            foreign.append(path)
            notes.append(f"keep: {path} still referenced by a surviving chart — left in place")
        elif ambiguous:
            foreign.append(path)
            notes.append(f"keep: {path} not removed due to an ambiguous chart reference")
        else:
            remove.append(path)

    # Foreign live charts (e.g. a watchdog's) — reported so the operator can see
    # mt4ctl is leaving them alone.
    for chart in remote_state.remote_chart_refs:
        if chart not in bundle_charts and chart not in managed:
            foreign.append(chart)

    return DeployPlan(
        add=tuple(add),
        update=tuple(update),
        remove=tuple(remove),
        unchanged=tuple(unchanged),
        foreign=tuple(sorted(set(foreign))),
        conflicts=tuple(conflicts),
        notes=tuple(notes),
    )
