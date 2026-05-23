"""mt4ctl — an MCP server for managing headless MetaTrader terminals.

Manage MetaTrader 4 terminals that run under Wine + systemd on remote hosts
(native Linux or WSL2) entirely over SSH: status, logs, screenshots, lifecycle
control, and headless first-login — exposed as Model Context Protocol tools.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
