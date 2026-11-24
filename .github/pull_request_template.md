<!-- Explain the change, the verification you performed, and any follow-up work. -->

## Summary

<!-- What does this PR change, and why? Link related issues or task references. -->

## Verification

Check the items that apply to this change; explain anything you skipped.

- [ ] Affected tests ran locally: `uv run --no-sync pytest -q -n auto <affected paths>` (record the exit code; list pre-existing failures separately)
- [ ] `uv run --no-sync ruff check <changed python paths>` and `uv run --no-sync ruff format --check <changed python paths>`
- [ ] `git diff --check` is clean for the changed files
- [ ] Bug fixes include a regression test; refactors reuse existing tests
- [ ] Frontend changes: `pnpm -C apps/web typecheck`, `pnpm -C apps/web build` (and the Desktop frontend when touched)
- [ ] User-visible flows: affected Playwright E2E suites pass
- [ ] Install, entry-point, or packaging changes: clean default install plus sdist/wheel build verified
- [ ] Prompt, glossary, polish, or review semantics changed: quality tradeoffs described
- [ ] Documentation updated when behavior, configuration, or contracts changed
- [ ] No real API keys or private book data committed; ignored local data left untouched

## Risks and follow-ups

<!-- Known limitations, migration notes, or planned follow-up work. -->
