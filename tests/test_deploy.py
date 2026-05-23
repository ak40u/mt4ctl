"""Pure deploy core: bundle reading, member safety, and the reconcile diff."""

from __future__ import annotations

from pathlib import Path

import pytest

from mt4ctl import deploy
from mt4ctl.deploy import RemoteState, compute_plan, read_bundle, validate_member
from mt4ctl.errors import BundleError, DeployError


# A minimal valid .chr referencing one expert, CRLF like a real MT4 file.
def _chr(ea_name: str) -> str:
    return "\r\n".join(["<chart>", "<expert>", f"name={ea_name}", "flags=343", "</expert>", ""])


def _make_bundle(tmp_path: Path, charts: dict[str, str], ex4s: list[str]) -> Path:
    """charts: {chart_filename: ea_name}; ex4s: list of relpaths under MQL4/Experts."""
    root = tmp_path / "bundle"
    cdir = root / "profiles" / "default"
    cdir.mkdir(parents=True)
    for fname, ea in charts.items():
        (cdir / fname).write_text(_chr(ea))
    for rel in ex4s:
        p = root / "MQL4" / "Experts" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"EX4-" + rel.encode())
    return root


# --------------------------------------------------------------------------- #
# read_bundle
# --------------------------------------------------------------------------- #
def test_read_bundle_hashes_files_and_maps_charts(tmp_path):
    root = _make_bundle(
        tmp_path,
        charts={"AUDUSD.chr": "SQ-29-03-2026\\SQ AUDUSD", "EURUSD.chr": "Old\\SQ EURUSD"},
        ex4s=["SQ-29-03-2026/SQ AUDUSD.ex4", "Old/SQ EURUSD.ex4"],
    )
    files, chart_to_ex4 = read_bundle(root)
    assert set(files) == {
        "profiles/default/AUDUSD.chr",
        "profiles/default/EURUSD.chr",
        "MQL4/Experts/SQ-29-03-2026/SQ AUDUSD.ex4",
        "MQL4/Experts/Old/SQ EURUSD.ex4",
    }
    assert all(len(h) == 64 for h in files.values())  # sha256 hex
    assert chart_to_ex4["profiles/default/AUDUSD.chr"] == "MQL4/Experts/SQ-29-03-2026/SQ AUDUSD.ex4"
    assert chart_to_ex4["profiles/default/EURUSD.chr"] == "MQL4/Experts/Old/SQ EURUSD.ex4"


def test_read_bundle_strips_experts_prefix_when_present(tmp_path):
    root = _make_bundle(
        tmp_path, charts={"x.chr": "Experts\\SQ\\Strat"}, ex4s=["SQ/Strat.ex4"]
    )
    _, chart_to_ex4 = read_bundle(root)
    assert chart_to_ex4["profiles/default/x.chr"] == "MQL4/Experts/SQ/Strat.ex4"


def test_read_bundle_integrity_guard_missing_ex4(tmp_path):
    root = _make_bundle(tmp_path, charts={"x.chr": "SQ\\Ghost"}, ex4s=[])
    with pytest.raises(BundleError, match="missing from the bundle"):
        read_bundle(root)


def test_read_bundle_ignores_stray_files(tmp_path):
    root = _make_bundle(tmp_path, charts={}, ex4s=["SQ/A.ex4"])
    (root / "README.txt").write_text("hi")
    (root / "MQL4" / "Experts" / "notes.md").write_text("nope")
    files, _ = read_bundle(root)
    assert set(files) == {"MQL4/Experts/SQ/A.ex4"}


def test_read_bundle_rejects_symlink(tmp_path):
    root = _make_bundle(tmp_path, charts={}, ex4s=["SQ/A.ex4"])
    link = root / "profiles" / "default" / "evil.chr"
    link.symlink_to(root / "MQL4" / "Experts" / "SQ" / "A.ex4")
    with pytest.raises(BundleError, match="symlink"):
        read_bundle(root)


def test_read_bundle_missing_dir(tmp_path):
    with pytest.raises(BundleError, match="not found"):
        read_bundle(tmp_path / "nope")


def test_read_bundle_rejects_non_bundle_dir(tmp_path):
    # a directory with neither managed root -> refuse (don't reconcile-to-empty)
    (tmp_path / "random").mkdir()
    (tmp_path / "random" / "notes.txt").write_text("x")
    with pytest.raises(BundleError, match="not a bundle"):
        read_bundle(tmp_path / "random")


# --------------------------------------------------------------------------- #
# validate_member
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "../escape.ex4",
        "MQL4/Experts/../../etc/passwd.ex4",
        "/abs/path.ex4",
        "profiles/other/x.chr",  # wrong profile dir
        "MQL4/Indicators/x.ex4",  # wrong subtree
        "profiles/default/x.set",  # wrong extension
        "",
    ],
)
def test_validate_member_rejects(name):
    with pytest.raises(BundleError):
        validate_member(name)


@pytest.mark.parametrize(
    "name",
    [
        "profiles/default/AUDUSD.chr",
        "MQL4/Experts/SQ-29-03-2026/SQ AUDUSD H4 0.15.ex4",
        "MQL4/Experts/flat.ex4",
    ],
)
def test_validate_member_accepts(name):
    validate_member(name)  # no raise


# --------------------------------------------------------------------------- #
# compute_plan — basic add / update / unchanged / remove
# --------------------------------------------------------------------------- #
def test_compute_plan_add_update_unchanged_remove():
    files = {
        "profiles/default/a.chr": "h_a",
        "MQL4/Experts/A.ex4": "h_A",  # unchanged
        "MQL4/Experts/B.ex4": "h_B_new",  # update
        "MQL4/Experts/C.ex4": "h_C",  # add (new)
    }
    chart_to_ex4 = {"profiles/default/a.chr": "MQL4/Experts/A.ex4"}
    state = RemoteState(
        deployed={
            "profiles/default/a.chr": "h_a",
            "MQL4/Experts/A.ex4": "h_A",
            "MQL4/Experts/B.ex4": "h_B_old",
            "MQL4/Experts/Z.ex4": "h_Z",  # managed, dropped from bundle -> remove
        },
        remote_hashes={
            "profiles/default/a.chr": "h_a",
            "MQL4/Experts/A.ex4": "h_A",
            "MQL4/Experts/B.ex4": "h_B_old",
            "MQL4/Experts/Z.ex4": "h_Z",
        },
    )
    plan = compute_plan(files, chart_to_ex4, state)
    assert plan.add == ("MQL4/Experts/C.ex4",)
    assert plan.update == ("MQL4/Experts/B.ex4",)
    assert plan.unchanged == ("MQL4/Experts/A.ex4", "profiles/default/a.chr")
    assert plan.remove == ("MQL4/Experts/Z.ex4",)
    assert plan.has_changes is True


def test_compute_plan_empty_when_identical():
    files = {"MQL4/Experts/A.ex4": "h"}
    state = RemoteState(deployed={"MQL4/Experts/A.ex4": "h"}, remote_hashes={"MQL4/Experts/A.ex4": "h"})
    plan = compute_plan(files, {}, state)
    assert plan.has_changes is False
    assert plan.unchanged == ("MQL4/Experts/A.ex4",)


# --------------------------------------------------------------------------- #
# compute_plan — manifest absence, conflict, corruption, drift
# --------------------------------------------------------------------------- #
def test_compute_plan_first_deploy_all_add():
    files = {"MQL4/Experts/A.ex4": "h", "profiles/default/a.chr": "hc"}
    plan = compute_plan(files, {"profiles/default/a.chr": "MQL4/Experts/A.ex4"}, RemoteState())
    assert set(plan.add) == set(files)
    assert plan.conflicts == ()


def test_compute_plan_unmanaged_collision_refuses():
    files = {"MQL4/Experts/A.ex4": "h"}
    # not in manifest, but the destination already exists on the host -> refuse
    state = RemoteState(deployed={}, dest_stats={"MQL4/Experts/A.ex4": True})
    plan = compute_plan(files, {}, state)
    assert plan.conflicts == ("MQL4/Experts/A.ex4",)
    assert plan.add == ()
    assert any("not managed" in n for n in plan.notes)


def test_parse_manifest_absent_and_marker():
    assert deploy.parse_manifest(None) is None
    assert deploy.parse_manifest("") is None
    assert deploy.parse_manifest("MISSING") is None


def test_parse_manifest_valid():
    raw = '{"version": 1, "files": {"MQL4/Experts/A.ex4": "h"}}'
    assert deploy.parse_manifest(raw) == {"MQL4/Experts/A.ex4": "h"}


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        '{"files": {}}',
        '{"version": 1}',
        '{"version": 1, "files": [1,2]}',
        '{"version": 2, "files": {}}',  # unsupported version → fail closed
        '"x"',
    ],
)
def test_parse_manifest_corrupt_raises(raw):
    with pytest.raises(DeployError):
        deploy.parse_manifest(raw)


# --------------------------------------------------------------------------- #
# build_manifest — canonical serializer (inverse of parse_manifest)
# --------------------------------------------------------------------------- #
def test_build_manifest_round_trips_through_parse():
    files = {
        "MQL4/Experts/SQ/A B.ex4": "h1",  # space in path
        'profiles/default/a"b.chr': "h2",  # quote in path
        "MQL4/Experts/Z.ex4": "h3",
    }
    raw = deploy.build_manifest(files)
    assert deploy.parse_manifest(raw) == files


def test_build_manifest_is_sorted_and_versioned():
    raw = deploy.build_manifest({"b.ex4": "2", "a.ex4": "1"})
    assert raw == '{"version":1,"files":{"a.ex4":"1","b.ex4":"2"}}'


def test_build_manifest_matches_apply_on_remote_format(tmp_path):
    # Parity: the Python serializer must emit byte-for-byte what build_apply_script
    # writes on the remote, so the two manifest writers can never drift. Run apply's
    # manifest-write (place/remove empty) against real files and compare.
    import hashlib
    import subprocess

    from mt4ctl import scripts

    d = tmp_path / "data"
    (d / ".mt4ctl").mkdir(parents=True)
    (d / "MQL4" / "Experts").mkdir(parents=True)
    (d / "profiles" / "default").mkdir(parents=True)
    (d / "MQL4" / "Experts" / "A.ex4").write_bytes(b"binary-a")
    (d / "profiles" / "default" / "a.chr").write_bytes(b"<chart>\r\n")
    rels = ["MQL4/Experts/A.ex4", "profiles/default/a.chr"]
    expected = {r: hashlib.sha256((d / r).read_bytes()).hexdigest() for r in rels}

    apply = scripts.build_apply_script(str(d), "stg", {}, [], sorted(rels), "root")
    assert subprocess.run(["bash", "-c", apply], capture_output=True).returncode == 0
    on_remote = (d / ".mt4ctl" / "deployed.json").read_text()
    assert on_remote == deploy.build_manifest(expected)


def test_compute_plan_drift_remote_wins():
    files = {"MQL4/Experts/A.ex4": "h_bundle"}
    # manifest says h_old, but on-disk is h_drift (someone changed it) -> update + drift note
    state = RemoteState(
        deployed={"MQL4/Experts/A.ex4": "h_old"},
        remote_hashes={"MQL4/Experts/A.ex4": "h_drift"},
    )
    plan = compute_plan(files, {}, state)
    assert plan.update == ("MQL4/Experts/A.ex4",)
    assert any("drift" in n for n in plan.notes)


def test_compute_plan_drift_missing_on_host_readds():
    files = {"MQL4/Experts/A.ex4": "h"}
    state = RemoteState(deployed={"MQL4/Experts/A.ex4": "h"}, remote_hashes={})
    plan = compute_plan(files, {}, state)
    assert plan.add == ("MQL4/Experts/A.ex4",)
    assert any("missing on host" in n for n in plan.notes)


# --------------------------------------------------------------------------- #
# compute_plan — .ex4 removal rules (the subtle ones)
# --------------------------------------------------------------------------- #
def test_compute_plan_same_short_name_different_folder_keyed_on_full_path():
    # Bundle keeps SQ/Strat.ex4 (+ its chart) but drops Old/Strat.ex4.
    files = {
        "profiles/default/sq.chr": "hc",
        "MQL4/Experts/SQ/Strat.ex4": "h1",
    }
    chart_to_ex4 = {"profiles/default/sq.chr": "MQL4/Experts/SQ/Strat.ex4"}
    state = RemoteState(
        deployed={
            "profiles/default/sq.chr": "hc",
            "MQL4/Experts/SQ/Strat.ex4": "h1",
            "MQL4/Experts/Old/Strat.ex4": "h2",  # different folder, same short name
        },
        remote_hashes={
            "profiles/default/sq.chr": "hc",
            "MQL4/Experts/SQ/Strat.ex4": "h1",
            "MQL4/Experts/Old/Strat.ex4": "h2",
        },
        remote_chart_refs={"profiles/default/sq.chr": "MQL4/Experts/SQ/Strat.ex4"},
    )
    plan = compute_plan(files, chart_to_ex4, state)
    assert plan.remove == ("MQL4/Experts/Old/Strat.ex4",)  # never the kept binary
    assert "MQL4/Experts/SQ/Strat.ex4" not in plan.remove


def test_compute_plan_shared_ex4_kept_until_all_charts_drop():
    # Two managed charts referenced one ex4; bundle drops one chart, keeps the other.
    files = {
        "profiles/default/b.chr": "hb",
        "MQL4/Experts/Shared.ex4": "hs",
    }
    chart_to_ex4 = {"profiles/default/b.chr": "MQL4/Experts/Shared.ex4"}
    state = RemoteState(
        deployed={
            "profiles/default/a.chr": "ha",
            "profiles/default/b.chr": "hb",
            "MQL4/Experts/Shared.ex4": "hs",
        },
        remote_hashes={
            "profiles/default/a.chr": "ha",
            "profiles/default/b.chr": "hb",
            "MQL4/Experts/Shared.ex4": "hs",
        },
        remote_chart_refs={
            "profiles/default/a.chr": "MQL4/Experts/Shared.ex4",
            "profiles/default/b.chr": "MQL4/Experts/Shared.ex4",
        },
    )
    plan = compute_plan(files, chart_to_ex4, state)
    assert plan.remove == ("profiles/default/a.chr",)  # only the dropped chart
    assert "MQL4/Experts/Shared.ex4" not in plan.remove  # still referenced by b


def test_compute_plan_ex4_referenced_by_foreign_chart_never_removed():
    # Managed ex4 dropped from bundle, but a FOREIGN (watchdog) chart references it.
    files: dict[str, str] = {}
    state = RemoteState(
        deployed={"MQL4/Experts/Shared.ex4": "hs"},
        remote_hashes={"MQL4/Experts/Shared.ex4": "hs"},
        remote_chart_refs={"profiles/default/watchdog.chr": "MQL4/Experts/Shared.ex4"},
    )
    plan = compute_plan(files, {}, state)
    assert "MQL4/Experts/Shared.ex4" not in plan.remove
    assert "MQL4/Experts/Shared.ex4" in plan.foreign
    assert "profiles/default/watchdog.chr" in plan.foreign  # the watchdog chart is reported


def test_compute_plan_ambiguous_reference_refuses_delete():
    files: dict[str, str] = {}
    state = RemoteState(
        deployed={"MQL4/Experts/Maybe.ex4": "hm"},
        remote_hashes={"MQL4/Experts/Maybe.ex4": "hm"},
        remote_chart_refs={"profiles/default/weird.chr": None},  # unparsable ref
    )
    plan = compute_plan(files, {}, state)
    assert "MQL4/Experts/Maybe.ex4" not in plan.remove
    assert "MQL4/Experts/Maybe.ex4" in plan.foreign
    assert any("ambiguous" in n for n in plan.notes)


def test_post_adopt_deploy_is_noop_and_preserves_watchdog():
    # Simulates the cutover: adopt recorded the bundle's footprint as the manifest;
    # a subsequent deploy of the SAME bundle must be a no-op AND leave a live
    # watchdog chart (+ its ex4) foreign, never removed.
    files = {
        "profiles/default/sq.chr": "hc",
        "MQL4/Experts/SQ/Strat.ex4": "he",
    }
    chart_to_ex4 = {"profiles/default/sq.chr": "MQL4/Experts/SQ/Strat.ex4"}
    state = RemoteState(
        deployed=dict(files),  # the manifest adopt wrote (bundle footprint only)
        remote_hashes=dict(files),  # on-disk matches the bundle
        remote_chart_refs={
            "profiles/default/sq.chr": "MQL4/Experts/SQ/Strat.ex4",
            "profiles/default/watchdog.chr": "MQL4/Experts/Watch/Dog.ex4",  # foreign, live
        },
    )
    plan = compute_plan(files, chart_to_ex4, state)
    assert plan.has_changes is False  # cutover then re-deploy == no-op
    assert "profiles/default/watchdog.chr" in plan.foreign
    assert "profiles/default/watchdog.chr" not in plan.remove


def test_compute_plan_lone_managed_chart_removed_with_its_ex4():
    files: dict[str, str] = {}
    state = RemoteState(
        deployed={
            "profiles/default/a.chr": "ha",
            "MQL4/Experts/A.ex4": "hA",
        },
        remote_hashes={
            "profiles/default/a.chr": "ha",
            "MQL4/Experts/A.ex4": "hA",
        },
        remote_chart_refs={"profiles/default/a.chr": "MQL4/Experts/A.ex4"},
    )
    plan = compute_plan(files, {}, state)
    assert set(plan.remove) == {"profiles/default/a.chr", "MQL4/Experts/A.ex4"}
    assert plan.foreign == ()
