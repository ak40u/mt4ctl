"""Enable ``python -m mt4ctl`` to run the CLI (defaults to the MCP server)."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
