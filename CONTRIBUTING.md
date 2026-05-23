# Contributing

Thanks for your interest in `mt4ctl`.

## Dev setup

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a PR

All three must pass (CI enforces them on Python 3.11–3.13):

```bash
ruff check src tests
mypy
pytest
```

## Conventions

- **Keep the core network-free.** Shell logic belongs in `scripts.py` /
  `login.py` as pure builders; `ssh.py` is the only module that executes
  commands. New behavior should be testable without a live host.
- **Type everything.** `mypy` runs in strict mode.
- **Actionable errors.** Raise the typed errors in `errors.py`; messages should
  tell the caller how to recover.
- **Never log secrets.** Passwords flow only through `auth.py` and the transient
  remote login config, which is shredded after use.
- **Conventional commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
  `chore:`).

## Adding a tool

1. Implement the operation in `operations.py` (or a focused module) against the
   `Registry`.
2. Add a thin `@mcp.tool()` wrapper in `server.py` with a clear docstring.
3. Cover the parsing / command construction with unit tests.
4. Document it in `docs/tools.md`.
