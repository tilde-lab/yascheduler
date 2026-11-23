# Summary

<!-- What changes and why. Name the failure or the requirement that motivates
it. Commits and the PR title follow Conventional Commits. -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor or cleanup
- [ ] Documentation
- [ ] CI, packaging or release tooling

## Verification

<!-- Exact commands and their outcome. A green check nobody can reproduce is
not verification. The integration and e2e suites need Docker. -->

- [ ] `uv run pytest -m unit`
- [ ] `uv run pytest -m integration`
- [ ] `uv run pytest -m e2e`
- [ ] `uv run zuban check`
- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run lint-imports`
- [ ] `uv run mdlint check`

## Contracts and specs

- [ ] `openspec/specs/` updated for every behavior change, and
      `openspec validate --all --json` passes
- [ ] A DB schema change ships its migration and the upgrade note it needs
- [ ] Public interfaces (CLI, INI config, Python API, plugin contract) stay
      compatible, or the break is named in Summary
- [ ] New architectural trade-off recorded in `docs/decisions/`
- [ ] `CHANGELOG.md` entry under `## Unreleased` for anything a user sees

<!-- Drop any unchecked line that does not apply and say why in Summary. -->
