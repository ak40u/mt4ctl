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

## Releasing

Releases publish to PyPI automatically via **Trusted Publishing** (OIDC — no API
tokens stored). One-time setup on PyPI: add a pending publisher for project
`mt4ctl`, owner `ak40u`, repo `mt4ctl`, workflow `release.yml`, environment
`pypi` (https://docs.pypi.org/trusted-publishers/).

To cut a release:

1. Bump `version` in `pyproject.toml` and `__version__` in
   `src/mt4ctl/__init__.py`; update `CHANGELOG.md`.
2. Commit, then tag and push:

   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```

`release.yml` verifies the tag matches the project version, builds + `twine
check`s the sdist/wheel, publishes to PyPI, and creates a GitHub Release with the
artifacts attached.

## Adding a tool

1. Implement the operation in `operations.py` (or a focused module) against the
   `Registry`.
2. Add a thin `@mcp.tool()` wrapper in `server.py` with a clear docstring.
3. Cover the parsing / command construction with unit tests.
4. Document it in `docs/tools.md`.
