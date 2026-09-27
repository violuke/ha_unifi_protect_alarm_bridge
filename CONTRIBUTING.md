# Contributing

## Tests

The integration supports Home Assistant 2026.9 and later, on Python 3.14:

```bash
uv venv .venv -p 3.14 && uv pip install --python .venv -r requirements_test.txt

.venv/bin/pytest
.venv/bin/pytest tests/test_api.py::test_expired_session_logs_in_again_once -v   # one test
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`tests/test_api.py` and `tests/test_websocket.py` run the real client against
`tests/fake_console.py`, a local aiohttp server. Test data must use placeholder
values only: see `tests/helpers.py`.

## CI

`.github/workflows/validate.yml` runs on every push and pull request:

- hassfest
- HACS validation
- ruff
- pytest

Pull requests must pass all of them.

## Trying it against a real console

```bash
scripts/develop
```

This starts Home Assistant on http://localhost:8123, with the integration
symlinked in. Add the integration through the UI. Credentials stay in the
gitignored `config/.storage`, so never put them in committed files.

## Releases

- Branch from `main` and open a pull request.
- A maintainer tags `main` as `vX.Y.Z` (or `vX.Y.Z-betaN` for a pre-release).
- `release.yml` stamps the manifest version, zips the integration, and
  publishes a GitHub release. Tags with a `-suffix` are marked as
  pre-releases, which HACS only shows to users who enable beta versions.
