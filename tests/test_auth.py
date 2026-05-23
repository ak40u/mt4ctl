"""Credential resolution chain."""

from __future__ import annotations

import json

import pytest

from mt4ctl import auth
from mt4ctl.errors import CredentialError


def test_explicit_password_wins(monkeypatch):
    monkeypatch.setenv("MT4CTL_PASSWORD_123", "from-env")
    assert auth.resolve_password("123", "explicit") == "explicit"


def test_env_var_used(monkeypatch):
    monkeypatch.setenv("MT4CTL_PASSWORD_123", "from-env")
    assert auth.resolve_password("123") == "from-env"


def test_secrets_file_used(monkeypatch, tmp_path):
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"123": "from-file"}))
    f.chmod(0o600)
    monkeypatch.delenv("MT4CTL_PASSWORD_123", raising=False)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", str(f))
    assert auth.resolve_password("123") == "from-file"


def test_missing_password_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("MT4CTL_PASSWORD_123", raising=False)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", str(tmp_path / "absent.json"))
    with pytest.raises(CredentialError, match="no password for account"):
        auth.resolve_password("123")


def test_corrupt_secrets_file_raises(monkeypatch, tmp_path):
    f = tmp_path / "creds.json"
    f.write_text("{not json")
    f.chmod(0o600)
    monkeypatch.delenv("MT4CTL_PASSWORD_123", raising=False)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", str(f))
    with pytest.raises(CredentialError, match="could not read"):
        auth.resolve_password("123")


def test_world_readable_secrets_file_rejected(monkeypatch, tmp_path):
    import os

    if os.name != "posix":
        pytest.skip("POSIX permission check")
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"123": "x"}))
    f.chmod(0o644)  # group/other readable
    monkeypatch.delenv("MT4CTL_PASSWORD_123", raising=False)
    monkeypatch.setenv("MT4CTL_CREDENTIALS", str(f))
    with pytest.raises(CredentialError, match="group/other"):
        auth.resolve_password("123")
