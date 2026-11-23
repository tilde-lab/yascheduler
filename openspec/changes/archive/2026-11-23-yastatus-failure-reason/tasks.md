# Tasks

Implementation checklist. Scope: the `-i` and `--json` renderers of
`yastatus`. The webhook payload and the AiiDA plugin are separate changes.

## 1. Renderers

- [x] 1.1 `--json`: add `error` to each task object, the failure reason or
  `null`.
- [x] 1.2 `-i`: end the line with an `error` field when the task has a
  failure reason, escaping backslash, tab, carriage return, and newline.

## 2. Verification

- [x] 2.1 Unit tests: a failed and a successful `DONE` task through `-i` and
  `--json`; the escaping of all four characters in an `-i` line.
- [x] 2.2 `pytest -m unit`, `zuban check`, `ruff check`,
  `ruff format --check`, `lint-imports`, and
  `openspec validate --all --json` pass.
