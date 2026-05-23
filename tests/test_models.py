"""Model invariants and lookups."""

from __future__ import annotations

import pytest

from mt4ctl.errors import UnknownTargetError
from mt4ctl.models import Env, Expert, Host, HostKind, TerminalStatus


def test_expert_short_name_strips_folder():
    assert Expert(name="SQ-29-03-2026\\SQ AUDUSD H4 0.157419", flags=343).short_name == (
        "SQ AUDUSD H4 0.157419"
    )


@pytest.mark.parametrize(
    ("flags", "expected"),
    [(343, True), (342, False), (0, False), (-1, None)],
)
def test_expert_live_trading_decode(flags, expected):
    assert Expert(name="x", flags=flags).live_trading is expected


def test_wsl_host_requires_distro():
    with pytest.raises(ValueError, match="wsl_distro"):
        Host(id="h", ssh="h", kind=HostKind.WSL)


def test_native_host_ok_without_distro():
    assert Host(id="h", ssh="h").kind is HostKind.NATIVE


def test_registry_groups_by_host(registry):
    grouped = registry.by_host()
    assert set(grouped) == {"demo-box", "live-vps"}
    assert [t.id for t in grouped["demo-box"]] == ["demo1"]


def test_host_of_resolves(registry):
    term = registry.terminal("live-main")
    assert registry.host_of(term).id == "live-vps"


def test_unknown_host_lookup_raises(registry):
    with pytest.raises(UnknownTargetError, match="unknown host"):
        registry.host("missing")


@pytest.mark.parametrize(
    ("state", "connected", "expected"),
    [
        ("active", True, True),
        ("active", False, False),
        ("inactive", True, False),
        ("active", None, False),
    ],
)
def test_status_health(state, connected, expected):
    st = TerminalStatus(
        id="t",
        host="h",
        env=Env.DEMO,
        account="1",
        service_state=state,
        connected=connected,
        log_age_seconds=1,
    )
    assert st.healthy is expected
