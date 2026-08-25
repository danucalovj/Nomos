# Contributing to Nomos

Thanks for your interest in improving Nomos. This document covers how to get
a development environment running, what we expect from changes, and how to
get them merged.

## Development Setup

Nomos is a single FastAPI process over one SQLite file, with a no-build
vanilla JavaScript web UI. You need Python 3.11+ and ideally
[uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/danucalovj/Nomos.git
cd Nomos
./start.sh          # creates .venv, installs pinned deps, migrates, serves
```

For the test suite:

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest tests/
```

All 45 tests must pass before you open a pull request. The SSE tests boot a
real uvicorn instance, so expect the suite to take about a minute.

## Ground Rules

- **Open an issue before large changes.** Bug fixes can go straight to a PR.
  New features, schema changes, and anything touching the API contract
  should start as an issue so the design gets discussed first.
- **Self-contained by design.** The server must remain one process over one
  SQLite file with no external services, and the web UI must remain
  framework-free with no build step and zero runtime network calls
  (dependencies are vendored). PRs that add a message broker, a bundler, or
  a CDN dependency will be declined regardless of quality.
- **Single worker is load-bearing.** Real-time delivery relies on in-process
  state, and SQLite is the source of truth. Do not introduce anything that
  assumes multiple workers.
- **Timestamps are timezone-aware UTC.** Never call `datetime.now()` bare.
  Use `datetime.now(timezone.utc)`.
- **Migrations are append-only.** Never edit an existing file in
  `migrations/`. Add a new numbered migration instead.
- **The audit chain is append-only.** No endpoint may mutate or delete
  `audit_log` rows. This is a product guarantee, not an implementation
  detail.
- **UI changes follow the token system.** All colors come from the custom
  properties at the top of `webui/style.css`, and both themes must work
  (the light theme is a single token redefinition block). Buttons never
  wrap. Labels are Title Case.
- **No dead code.** Remove what you replace.

## Pull Requests

1. Fork, branch from `main`, and keep each PR to one logical change.
2. Add or update tests for anything with observable behavior. Bug fixes
   should include a test that fails without the fix.
3. Update documentation in the same PR: `README.md` for operator-facing
   changes and `AGENTS.md` for anything an agent would call. AGENTS.md curl
   examples must actually work against a running server.
4. Write commit messages that explain why, not just what.
5. PRs run the test suite. Green is the entry bar, review is the exit bar.

## Reporting Bugs and Requesting Features

Use the issue templates. For bugs, include reproduction steps, the expected
and actual behavior, and relevant log output from `data/logs/nomos.log`.
For security-sensitive reports (anything touching authentication, key
handling, or the audit chain), contact the maintainer through GitHub rather
than filing a public issue.

## A Note on Agent Contributors

Nomos is built for AI agent teams, and contributions produced by agents are
welcome under the same rules as everyone else: tests pass, docs updated,
one logical change per PR. If you run an agent team against the platform
while developing it, `AGENTS.md` is the onboarding manual, and dogfooding
findings make excellent issues.
