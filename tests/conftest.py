"""Shared fixtures."""

from __future__ import annotations

import pytest

from mt4ctl.config import parse_registry
from mt4ctl.models import Registry

SAMPLE = {
    "hosts": {
        "demo-box": {
            "ssh": "demo-box",
            "kind": "wsl",
            "wsl_distro": "Ubuntu-24.04",
            "broker_host": "demo.broker.example.com",
        },
        "live-vps": {"ssh": "live-vps", "kind": "native"},
    },
    "terminals": {
        "demo1": {
            "host": "demo-box",
            "service": "mt4-demo1",
            "data_dir": "/home/trader/mt4/demo1",
            "display": ":99",
            "account": "1000001",
            "env": "demo",
        },
        "live-main": {
            "host": "live-vps",
            "service": "mt4-live-main",
            "data_dir": "/home/trader/mt4/Live Terminal 2000001",
            "display": ":2",
            "account": "2000001",
            "env": "live",
        },
    },
}


@pytest.fixture
def registry() -> Registry:
    return parse_registry(SAMPLE)
