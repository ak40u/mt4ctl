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
