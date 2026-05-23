"""Registry parsing and validation."""

from __future__ import annotations

import pytest

from mt4ctl.config import load_registry, parse_registry, resolve_path
from mt4ctl.errors import ConfigError, UnknownTargetError
from mt4ctl.models import Env, HostKind

VALID_YAML = """
hosts:
  h: {ssh: h}
terminals:
  t: {host: h, service: s, data_dir: /d, account: "1"}
"""


def test_parses_hosts_and_terminals(registry):
    assert set(registry.hosts) == {"demo-box", "live-vps"}
    assert registry.hosts["demo-box"].kind is HostKind.WSL
    assert registry.hosts["demo-box"].wsl_distro == "Ubuntu-24.04"
    assert registry.terminal("live-main").env is Env.LIVE
    assert registry.terminal("demo1").account == "1000001"


def test_data_dir_with_spaces_preserved(registry):
    assert registry.terminal("live-main").data_dir.endswith("Live Terminal 2000001")


def test_window_query_falls_back_to_account(registry):
    assert registry.terminal("demo1").window_query == "1000001"


def test_missing_hosts_section_rejected():
    with pytest.raises(ConfigError, match="no hosts"):
        parse_registry({"terminals": {}})


def test_terminal_referencing_unknown_host_rejected():
    data = {
        "hosts": {"h": {"ssh": "h"}},
        "terminals": {"t": {"host": "nope", "service": "s", "data_dir": "/d"}},
    }
    with pytest.raises(ConfigError, match="unknown host"):
        parse_registry(data)


def test_wsl_host_without_distro_rejected():
    data = {
        "hosts": {"h": {"ssh": "h", "kind": "wsl"}},
        "terminals": {"t": {"host": "h", "service": "s", "data_dir": "/d"}},
    }
    with pytest.raises(ConfigError, match="wsl_distro"):
        parse_registry(data)


def test_invalid_kind_rejected():
    data = {
        "hosts": {"h": {"ssh": "h", "kind": "vm"}},
        "terminals": {"t": {"host": "h", "service": "s", "data_dir": "/d"}},
    }
    with pytest.raises(ConfigError, match="invalid kind"):
        parse_registry(data)


def test_missing_required_terminal_field_rejected():
    data = {
        "hosts": {"h": {"ssh": "h"}},
        "terminals": {"t": {"host": "h", "service": "s"}},  # no data_dir
    }
    with pytest.raises(ConfigError, match="data_dir"):
        parse_registry(data)


def test_unknown_terminal_lookup_lists_known(registry):
    with pytest.raises(UnknownTargetError, match="demo1"):
        registry.terminal("ghost")


def test_load_registry_from_explicit_path(tmp_path):
    f = tmp_path / "terminals.yaml"
    f.write_text(VALID_YAML)
    reg = load_registry(f)
    assert reg.terminal("t").service == "s"


def test_resolve_path_honours_env(tmp_path, monkeypatch):
    f = tmp_path / "custom.yaml"
    f.write_text(VALID_YAML)
    monkeypatch.setenv("MT4CTL_CONFIG", str(f))
    assert resolve_path() == f


def test_load_registry_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MT4CTL_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # nothing there
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="no registry file found"):
        load_registry()


def test_load_registry_explicit_missing_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_registry(tmp_path / "absent.yaml")


def test_load_registry_empty_file_raises(tmp_path):
    f = tmp_path / "terminals.yaml"
    f.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_registry(f)


def test_load_registry_invalid_yaml_raises(tmp_path):
    f = tmp_path / "terminals.yaml"
    f.write_text("hosts: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_registry(f)
