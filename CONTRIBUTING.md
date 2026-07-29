# Contributing to byoai-runtime

Thanks for your interest in contributing! This document covers how to get a dev environment
running, the checks your change needs to pass, and how PRs get reviewed and released.

## Development setup

```bash
git clone https://github.com/ravikings/byoai-runtime.git
cd byoai-runtime
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[all,dev]"
```

`all` pulls in every optional integration (FastAPI, Robyn, Redis, pgvector, OTel) so the full
test suite can run; `dev` adds pytest, ruff, pyright, and packaging tools.

## Branching

Do all work on a feature branch off `main` — never commit directly to `main`:

```bash
git checkout main && git pull
git checkout -b feat/<short-description>   # or fix/<short-description>
```

## Checks

Run these before opening a PR — CI runs the same checks on every push and PR:

```bash
ruff check .        # lint
pyright             # type check
python -m pytest    # tests (use `python -m pytest`, not bare `pytest` —
                     # the test modules import `tests.conftest`, which needs
                     # the repo root on sys.path)
```

Tests live under `tests/`, mirroring the module they cover (e.g. `src/byoai/cache/redis.py` →
`tests/test_cache_memory.py` / integration tests). New runtime behavior needs test coverage;
bug fixes should include a regression test.

## Commit messages and PRs

- Keep commits focused — one logical change per commit.
- Write commit messages that explain *why*, not just *what*.
- Update `CHANGELOG.md` under `[Unreleased]` for any user-facing change (new feature, behavior
  change, bug fix, deprecation).
- Open a PR against `main`. Fill in the PR template — link any related issue, and describe how
  you tested the change.

## AI-assisted development

This project welcomes AI-assisted contributions (code, tests, docs, debugging). Regardless of
how a change was produced:

- All contributions are reviewed by maintainers before merge.
- All code must meet the project's quality bar (lint, types, tests).
- Runtime changes require tests.
- Security-sensitive changes require human review — see [SECURITY.md](SECURITY.md).
- You remain responsible for code you submit, however it was produced.

## Code of Conduct

Participation in this project is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Release process

Maintainers bump `version` in `pyproject.toml`, tag `vX.Y.Z` (matching that version exactly),
and publish a GitHub Release; the [`publish.yml`](.github/workflows/publish.yml) workflow
verifies the tag matches `pyproject.toml`, then builds and publishes to PyPI automatically.
Contributors don't need to do anything release-related beyond keeping `CHANGELOG.md` up to date.
