"""Bash builders (pure)."""

from __future__ import annotations

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
