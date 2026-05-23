"""Bash builders (pure)."""

from __future__ import annotations

import pytest

from mt4ctl import scripts


def test_sh_quote_escapes_single_quotes():
    assert scripts.sh_quote("a'b") == "'a'\\''b'"
    assert scripts.sh_quote("plain") == "'plain'"


def test_status_script_includes_each_terminal_and_broker():
    specs = [("demo1", "mt4-demo1", "/home/t/demo1")]
    out = scripts.build_status_script("broker.example.com", specs)
    assert "emit 'demo1' 'mt4-demo1' '/home/t/demo1'" in out
    assert "broker.example.com" in out
    assert "cgroup.procs" in out  # per-terminal socket attribution


def test_status_script_handles_no_broker():
    out = scripts.build_status_script(None, [("d", "s", "/d")])
    assert "BROKER=''" in out


def test_logs_script_uses_grep_when_pattern_given():
    out = scripts.build_logs_script("/d", "login|error", 20)
    assert "grep -aiE 'login|error'" in out
    assert "tail -n 20" in out


def test_logs_script_tails_when_no_pattern():
    out = scripts.build_logs_script("/d", None, 10)
    assert "tail -n 10" in out
    assert "grep" not in out


def test_logs_script_quotes_dangerous_data_dir_everywhere():
    # The no-log message must use the quoted $DIR, never the raw value.
    out = scripts.build_logs_script("x$(touch /tmp/pwned)", None, 10)
    assert "DIR='x$(touch /tmp/pwned)'" in out
    assert "printf " in out and '"$DIR"' in out
    assert "(no log files in x$(touch" not in out  # no raw interpolation


def test_status_script_handles_broker_failure_and_visibility():
    out = scripts.build_status_script("broker.example.com", [("d", "s", "/d")])
    assert "BROKER_FAIL" in out
    assert "getent ahosts" in out  # IPv4 + IPv6
    # attribution is gated on root or matching service user, else unknown (-1)
    assert "attrib=1" in out
    assert "svcuser=$(systemctl show -p User" in out


def test_experts_script_reads_master_and_charts():
    out = scripts.build_experts_script("/home/t/demo1")
    assert "config/terminal.ini" in out
    assert "^Experts=" in out
    assert "profiles/default/*.chr" in out
    assert "MASTER|" in out and "EA|" in out


def test_info_script_extracts_build_and_ping():
    out = scripts.build_info_script("/home/t/demo1")
    assert "build [0-9]+" in out
    assert "ping:" in out
    assert "BUILD|" in out and "LOGIN|" in out


def test_experts_script_strips_crlf():
    out = scripts.build_experts_script("/home/t/demo1")
    assert "sub(/\\r$/" in out  # CRLF guard so flags parse and lines don't over-split


def test_doctor_script_probes_tools_and_terminals():
    out = scripts.build_doctor_script([("t1", "mt4-t1", "/home/t/t1")])
    assert "command -v" in out
    assert "systemctl cat" in out
    assert "checkterm 't1' 'mt4-t1' '/home/t/t1'" in out


def test_control_script_rejects_bad_action():
    with pytest.raises(ValueError, match="action must be"):
        scripts.build_control_script("svc", "frobnicate")


def test_control_script_propagates_exit_code():
    out = scripts.build_control_script("mt4-x", "restart")
    assert "exit $rc" in out


def test_status_script_captures_state_without_extra_line():
    # `is-active` prints inactive/failed AND exits non-zero; `|| echo unknown`
    # would append a second line and corrupt the protocol.
    out = scripts.build_status_script(None, [("d", "s", "/d")])
    assert "|| echo unknown" not in out
    assert '[ -n "$state" ] || state=unknown' in out


def test_control_script_captures_state_without_extra_line():
    out = scripts.build_control_script("svc", "restart")
    assert "|| echo unknown" not in out
    assert '[ -n "$st" ] || st=unknown' in out


def test_control_script_reports_state():
    out = scripts.build_control_script("mt4-x", "restart")
    assert "systemctl restart 'mt4-x'" in out
    assert "STATE|" in out


def test_screenshot_script_activates_window_when_query_given():
    out = scripts.build_screenshot_script(":99", "1000001", "/tmp/x.png")
    assert "DISPLAY=':99'" in out
    assert "xdotool search --name '1000001'" in out
    assert "import -window root '/tmp/x.png'" in out


def test_screenshot_script_skips_activate_without_query():
    out = scripts.build_screenshot_script(":0", None, "/tmp/x.png")
    assert "xdotool search" not in out


# --------------------------------------------------------------------------- #
# Deploy builders
# --------------------------------------------------------------------------- #
def test_remote_state_emits_user_manifest_charts_and_dest_stats():
    out = scripts.build_remote_state_script(
        "/home/t/d", ["MQL4/Experts/A.ex4", "profiles/default/a.chr"], "mt4-x"
    )
    assert "systemctl show -p User --value \"$UNIT\"" in out
    assert "[ -z \"$U\" ] && U=root" in out  # empty User= -> root
    assert "UNIT='mt4-x'" in out
    assert f"$DIR/{scripts.MANIFEST_REL}" in out
    assert "MANIFEST|MISSING" in out
    assert "base64" in out  # manifest shipped base64 (binary-safe)
    assert "profiles/default/*.chr" in out  # live chart enumeration
    assert "emit_dest 'MQL4/Experts/A.ex4'" in out
    assert "emit_dest 'profiles/default/a.chr'" in out
    assert "DEST|" in out and "CHART|" in out


def test_remote_state_quotes_dangerous_data_dir():
    out = scripts.build_remote_state_script("x$(touch /tmp/pwn)", [], "u")
    assert "DIR='x$(touch /tmp/pwn)'" in out


def test_drain_polls_cgroup_and_fails_on_persisting_proc():
    out = scripts.build_drain_script("mt4-x", 20)
    assert "cgroup.procs" in out
    assert "terminal.exe|terminal64.exe" in out
    assert "DRAIN|ok" in out
    assert "DRAIN|timeout" in out
    assert "exit 1" in out  # caller aborts apply on persisting process
    assert "seq 1 20" in out


def test_backup_includes_manifest_and_prunes_to_three():
    out = scripts.build_backup_script("/home/t/d", ["MQL4/Experts/A.ex4"], "20260523-0000")
    assert "-T \"$LIST\"" in out  # tar from the gathered file list
    assert scripts.MANIFEST_REL in out  # manifest tarred alongside managed files
    assert "TS='20260523-0000'" in out and '"$BK/$TS.tar"' in out
    assert "tail -n +4" in out  # keep newest 3
    assert "'MQL4/Experts/A.ex4'" in out  # path quoted


def test_backup_noop_when_nothing_managed():
    out = scripts.build_backup_script("/home/t/d", [], "ts")
    assert "BACKUP|none" in out


def test_apply_verifies_staged_hashes_before_move_and_writes_manifest_last():
    place = {"MQL4/Experts/A.ex4": "deadbeef", "profiles/default/a.chr": "cafe"}
    out = scripts.build_apply_script(
        "/home/t/d",
        f"{scripts.STAGING_DIR}/u1",
        place,
        ["MQL4/Experts/Old.ex4"],
        ["MQL4/Experts/A.ex4", "profiles/default/a.chr"],
        "trader",
    )
    # staged-hash verification appears, and its abort precedes any mv in the text
    assert "APPLY|hashfail" in out
    assert '[ "$got" = \'deadbeef\' ]' in out
    hashfail_at = out.index("APPLY|hashfail")
    first_mv = out.index("mv -f")
    assert hashfail_at < first_mv  # verify-before-move
    # manifest written from on-disk hashes, LAST (after the moves/removes)
    assert out.index("mv -f") < out.index('"version":1')
    assert "rm -f \"$DIR/\"'MQL4/Experts/Old.ex4'" in out
    assert 'mv -f "$TMP" "$DIR/' + scripts.MANIFEST_REL in out
    # chown to the unit user (so MT4 can rewrite on exit)
    assert "chown -R \"$U\"" in out
    assert "U='trader'" in out


def test_apply_skips_chown_for_root_unit():
    out = scripts.build_apply_script("/d", "s", {}, [], [], "root")
    assert '[ "$U" != root ]' in out  # chown guarded out when unit runs as root


def test_apply_quotes_filenames_with_shell_metacharacters():
    # a hostile/odd .ex4 name must never break out of the script or its quoting
    import subprocess

    evil = 'MQL4/Experts/a";id #.ex4'
    out = scripts.build_apply_script(
        "/home/t/d", "stg", {evil: "h"}, [evil], [evil], "trader"
    )
    assert 'echo "APPLY|hashfail|$f"' in out  # message uses a quoted var, never raw rel
    # every occurrence of the name is single-quoted (sh_quote or json+sh_quote);
    # a clean `bash -n` parse proves no metacharacter escapes its quoting.
    assert subprocess.run(["bash", "-n"], input=out.encode()).returncode == 0


def test_manifest_put_ships_blob_base64_and_writes_atomically():
    import base64

    out = scripts.build_manifest_put_script("/home/t/d", '{"version":1,"files":{}}', "trader")
    assert "base64 -d" in out
    assert base64.b64encode(b'{"version":1,"files":{}}').decode() in out  # blob shipped b64
    assert '{"version":1' not in out  # raw JSON never crosses the wire unencoded
    assert 'mv -f "$TMP" "$DIR/' + scripts.MANIFEST_REL in out  # atomic write
    assert "ADOPT|ok" in out
    assert "U='trader'" in out
    assert 'chown "$U"' in out
    assert "systemctl" not in out  # never stops/starts; records-only


def test_manifest_put_skips_chown_for_root_unit():
    out = scripts.build_manifest_put_script("/d", "{}", "root")
    assert '[ "$U" != root ]' in out  # don't chown to root


def test_manifest_put_is_valid_bash():
    import subprocess

    out = scripts.build_manifest_put_script("/home/t/d", '{"version":1,"files":{"a":"h"}}', "trader")
    assert subprocess.run(["bash", "-n"], input=out.encode()).returncode == 0


def test_lock_acquire_is_atomic_mkdir_with_stale_takeover():
    out = scripts.build_lock_acquire_script("/home/t/d", "host:1234", 600)
    assert 'mkdir "$LD"' in out  # atomic mkdir lock
    assert scripts.LOCKDIR in out
    assert "LOCK|acquired" in out
    assert "LOCK|held" in out
    assert "LOCK|takeover" in out
    assert "- ts)) -gt 600" in out  # stale threshold
    assert "H='host:1234'" in out


def test_lock_release_only_when_owner_matches():
    out = scripts.build_lock_release_script("/home/t/d", "host:1234")
    assert 'owner=$(awk' in out
    assert '[ "$owner" = "$H" ]' in out
    assert "LOCK|released" in out
    assert "LOCK|notowner" in out


def test_restore_purges_touched_then_extracts_backup():
    out = scripts.build_restore_script(
        "/home/t/d", "/home/t/d/.mt4ctl/backups/ts.tar", ["MQL4/Experts/New.ex4"], "trader"
    )
    rm_at = out.index("rm -f \"$DIR/\"'MQL4/Experts/New.ex4'")
    tar_at = out.index("tar -C \"$DIR\" -xf")
    assert rm_at < tar_at  # purge touched paths BEFORE re-extracting backup
    assert "--no-same-owner" in out and "--no-absolute-names" not in out
    assert "RESTORE|done" in out


def test_restore_purge_only_when_no_backup():
    out = scripts.build_restore_script("/home/t/d", None, ["MQL4/Experts/New.ex4"], "trader")
    assert "rm -f \"$DIR/\"'MQL4/Experts/New.ex4'" in out
    assert "tar -C" not in out  # first deploy: nothing to extract, purge only
    assert "RESTORE|done" in out


def test_deploy_preflight_checks_tar_sha_and_same_device():
    out = scripts.build_deploy_preflight_script("/home/t/d")
    assert "tar --version" in out and "grep -qi gnu" in out
    assert "sha256sum" in out and "openssl" in out
    assert "stat -c %d" in out
    assert "PRE|tar|" in out and "PRE|sha|" in out and "PRE|device|" in out
