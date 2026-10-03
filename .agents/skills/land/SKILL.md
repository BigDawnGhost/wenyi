---
name: land
description: >-
  Land an explicitly requested Wenyi change through a new remote branch and a
  pull request targeting dev, with complete CI verification before merging.
  Invoke only when the user requests landing or merging, including an explicit
  /land invocation, not merely for review, preparation, passing checks, or
  installing this skill.
disable-model-invocation: true
metadata:
  delta-action: land
---

# Land a Wenyi change

Carry the requested change through preparation, verification, publication, and
merge into `dev`. Creating a commit, pushing a branch, opening a PR, or starting
CI is not completion. An explicit invocation supplies landing intent: proceed
without asking for the same merge permission again. Stop for genuine blockers,
unclear scope, unmet automatic landing gates, or unsafe operations.

This workflow applies to `BigDawnGhost/wenyi`. The expected publication remote
is `origin`, currently `https://github.com/BigDawnGhost/wenyi.git`; verify this
at execution time. Never publish through the `local` checkout backlink.

## 1. Establish scope and prerequisites

- Read the current root and applicable nested `AGENT.md` / `AGENTS.md`,
  `CONTRIBUTING.md`, submission templates, and relevant workflow definitions.
  Their applicable requirements remain binding even without server enforcement.
  The translation-quality evaluation criterion is assigned to owner review,
  rather than automatic landing checks, as specified in section 2.
- Inspect status, staged and unstaged diffs, untracked file names, current branch,
  upstream, and outgoing commits. Identify precisely which files and commits
  belong to the requested change. Do not absorb unrelated changes merely because
  they are already staged or lie on the current branch.
- Check Git author identity, signing configuration, hooks, tool versions, and
  GitHub authentication. Do not invent an author, disable signing or hooks,
  change global configuration, expose credentials, or bypass missing access.
  Installing system tools or changing services outside the workspace requires
  separate authorization.
- Use `git` and `gh` directly; the project has no authoritative landing wrapper.
  Use non-interactive commands with explicit arguments. Prefix every `git
  commit` or `git merge` invocation with `GIT_EDITOR=true`; do not launch editors.

Read-only preflight:

```sh
git --no-optional-locks status --short
git diff --stat
git diff --cached --stat
git branch -vv
git remote -v
gh auth status --active --hostname github.com
gh api repos/BigDawnGhost/wenyi
gh api repos/BigDawnGhost/wenyi/branches/dev
gh api repos/BigDawnGhost/wenyi/branches/dev/protection
gh api repos/BigDawnGhost/wenyi/rules/branches/dev
```

Inspect only the relevant repository, access, merge-method, check, and review
settings from these responses. A specifically confirmed unprotected branch is
not an authentication failure, but other errors or unverifiable requirements
are blockers. Recheck these settings immediately before merging. Do not assume
the protections or permissions observed during setup remain unchanged.

## 2. Enforce contribution requirements

Sources: `AGENTS.md` sections "验证与完成", "代码与文档风格", and "Git 与交付";
`CONTRIBUTING.md` and `docs/zh/CONTRIBUTING.md`.

- Use Conventional Commits. Explain the behavior change, verification, known
  limitations, and excluded local files in the PR. Reference only real related
  issues; do not invent issues, approvals, sign-offs, or closing relationships.
- Keep affected English and Chinese user documentation synchronized. Check
  configuration defaults, API schemas/shared types, and frontend callers when
  their corresponding behavior changes.
- Translation-quality acceptance, including the contribution guide's long-form
  public-domain comparison criterion, belongs to the owner during PR review.
  It is not an automatic landing prerequisite in this workflow. A missing
  quality comparison alone must not block branch creation, commits, publication,
  CI, or merging, or cause the agent to stop and demand an evaluation.
- For changes that can affect translation quality, include any available
  comparison evidence in the PR, or explicitly state that real-model quality
  evaluation has not been performed. Do not present offline fixtures or browser
  tests as evidence of literary quality. The owner decides whether more quality
  evaluation is needed; honor actual review decisions and all other applicable
  review requirements without inventing approval.
- Do not initiate paid model evaluation just to complete a landing checklist.
  Obtain separate authorization for the evaluation scope, data, and budget.
  Do not read private books or publish book content without authorization.
- Apply any current signing, review, submission, or contributor requirements
  with their stated conditions and exceptions. Ask only about unmet applicable
  requirements, not ones already established.

## 3. Audit and clean redundant files safely

Perform a cleanup audit before the first commit and again before the final
merge. This is a targeted cleanup, not permission to purge the working tree.

- Inventory file names and diffs first. Check whether a candidate is tracked,
  referenced, task-owned, and actually redundant. Being untracked, ignored,
  empty, old, or named `tmp` is not sufficient evidence for deletion.
- Automatically remove only disposable temporary files created by this landing
  run whose ownership and lack of further use are certain. Preserve any files
  still needed for verification, recovery, or reporting.
- For existing redundant source files, generated artifacts, or files of unclear
  ownership, present the exact proposed deletion list and reasons and obtain
  approval before deleting. Already approved task-specific deletions do not
  require repeat permission.
- Preserve unrelated changes and local data, especially `state/`, `output/`,
  `review-*`, Web `DATA_DIR`, `.env` files, databases, books, caches, and existing
  build/test outputs. Do not read private book contents to classify them.
- Never use blanket `git clean`, `git reset --hard`, or recursive directory
  purges. Do not delete local or remote branches automatically.
- Include approved tracked-file removals in the reviewed diff and verification.
  If cleanup changes tested source or configuration, rerun the affected checks
  and require CI on the new final commit. Report retained cleanup candidates
  separately; unrelated retained files need not prevent landing a safe change.

Source: `AGENTS.md` local-data preservation and Git scope rules; `.gitignore`
and `apps/web/.gitignore` identify excluded artifacts, not deletion permission.

## 4. Prepare a new branch and integrate dev

- Fetch the intended publication remote, including `dev`, without rewriting
  local branches. Choose a new, unused descriptive branch such as
  `feat/three-draft-precision`; verify it does not already exist locally or
  remotely. Do not push directly to `dev` or `main`.
- Create the branch from the change's appropriate current history. If that
  history includes unrelated outgoing commits, stop to separate the scope
  rather than publishing them.
- Stage only explicitly reviewed task paths, never blanket `git add .`.
  Create a Conventional Commit with an explicit message. Respect hook changes:
  inspect them, stage only in-scope results, and rerun invalidated checks.
- Merge the latest `origin/dev` into this new branch, retaining shared history.
  Do not rebase or force-push published history.
- Resolve conflicts automatically when the intended result is clear. Preserve
  both the requested behavior and unrelated upstream changes. Pause and describe
  conflicts whose intent is ambiguous or whose resolution would alter unrelated
  work; do not choose an entire side indiscriminately.
- Never stash, discard, or commit unrelated dirty files to make integration
  convenient. If they prevent safe integration or verification of the exact
  candidate, stop for separation.

For the verified remote and a selected unused `$branch`, the sequence uses:

```sh
git fetch origin
git switch -c "$branch"
# Stage only the explicitly reviewed task paths.
GIT_EDITOR=true git commit -m "$commit_message"
GIT_EDITOR=true git merge --no-edit origin/dev
```

Skip the commit step when the requested change is already committed and no
in-scope staged changes remain. Review the complete resulting diff against
`origin/dev`, not just the last commit. Record the candidate head and integrated
base SHAs. Source: `AGENTS.md` Git rules; merge commits follow current repository
practice, subject to the destination's current allowed methods and rules.

## 5. Verify the candidate locally

Use a tree that represents the candidate commit. Uncommitted source changes
must not make a test pass for code that will not be published. Keep local checks
proportional to the change as required by `AGENTS.md`; the complete remote
`Tests` workflow is still required for every landing.

Before every landing, run full-scope Ruff lint/format checks and Pyright for
the repository's configured Python source and test scope, not just changed
files. Require zero errors and zero warnings. Fix findings and rerun; do not
disable diagnostics, expand exclusions, or add blanket suppressions to pass.
Missing checker tooling or unverifiable results are blockers.

### Python

For Python changes, install the development workspace and run the affected
tests, Ruff lint/format checks, and diff checks. State, locking, resume,
cross-domain, and other high-risk changes require the full Python suite:

```sh
uv sync --locked --all-packages --group dev --python 3.12
uv run --no-sync ruff check packages/core packages/cli apps/api
uv run --no-sync ruff format --check packages/core packages/cli apps/api
pyright --project pyrightconfig.json --warnings
uv run --no-sync pytest -q
git diff --check
```

Sources: `.github/workflows/tests.yml` workspace installation and Python checks;
`pyproject.toml` workspace/dev dependencies, Ruff settings, and pytest test paths;
`AGENTS.md` adds format checking and defines verification scope.
`pyrightconfig.json` defines the full Core/CLI/API source and test scope and
the workspace virtual environment. Pyright's `--warnings` makes warnings fail
the command; also verify the diagnostic summary reports zero errors and
warnings. Record the checker version. The current remote workflow does not run
Pyright, so its success cannot substitute for this explicit local gate.

Run architecture boundaries for dependency changes, orchestrator contracts for
pipeline wiring, and storage injection plus backend tests for storage changes.
The full suite includes these tests via the configured pytest paths.

Use Python 3.10 or 3.12, matching CI, rather than assuming the system Python is
suitable. After installing all workspace packages, preserve them with
`uv run --no-sync`. If the uv cache is not writable, use the documented
per-command `UV_CACHE_DIR` override; do not change `HOME`.

PostgreSQL/Redis tests must use isolated test services through
`WENYI_TEST_DATABASE_URL` and `WENYI_TEST_REDIS_URL`, never application or
production services. If unavailable locally, report the backend skips as
unverified locally and require the remote service-backed tests to run and pass.
Do not start or reset the project's ordinary deployment databases for testing.

### Web and API

For frontend changes, use Node 22 and pnpm 9, as specified by the current CI
configuration. A different installed major version does not establish CI parity.

```sh
pnpm install --frozen-lockfile
pnpm -C apps/web typecheck
pnpm -C apps/web build
pnpm -C apps/web test:e2e
```

Sources: `.github/workflows/tests.yml` web job; `apps/web/package.json` defines
the exact `typecheck`, `build`, and `test:e2e` scripts;
`pnpm-workspace.yaml` defines the workspace.

For local runs, affected E2E tests may be selected according to the change;
remote CI must run the full suite. If bundled Chromium is absent, a verified
existing system Chromium can be selected with
`PLAYWRIGHT_CHROMIUM_EXECUTABLE`, supported by
`apps/web/playwright.config.ts`. Otherwise arrange browser prerequisites
without silently installing system packages or altering user services.
Record which browser was used.

Interface, authentication, and progress changes require API and frontend
verification. Check backend OpenAPI, shared types, and consumers together.
`package.json` defines `pnpm gen:schema`; it requires the candidate API already
running on `localhost:8000`. Do not generate from an arbitrary existing server
or connect it to real user data. Inspect and commit any in-scope regenerated
types, then rerun invalidated checks.

### Installation and packaging

For installation, entry-point, or packaging changes, require the clean CLI
installation, entry-point smoke tests, package builds, version checks, and
resource checks specified by `AGENTS.md`. The remote Python jobs run these in
fresh environments:

- `.github/workflows/tests.yml` defines `uv sync --locked --no-dev --python`
  with the matrix version and a fresh `UV_PROJECT_ENVIRONMENT`, followed by
  `python -m wenyi_cli --version`, `wenyi --version`, and `wenyi --help` through
  `uv run --no-sync`.
- Its Python 3.12 job runs `uv build --package wenyi-core`,
  `uv build --package wenyi-cli`, `uv build --package wenyi-api`, and
  `uv run --no-sync python scripts/check_wheel_resources.py dist`.
- Package names, CLI entry points, and VCS version configuration are defined in
  `packages/core/pyproject.toml`, `packages/cli/pyproject.toml`, and
  `apps/api/pyproject.toml`. `scripts/check_wheel_resources.py` accepts the
  output directory as its positional argument.

Do not overwrite or delete existing local environments or build artifacts to
imitate a fresh runner. Use a new isolated workspace-owned validation location
when needed, or obtain permission for any conflicting setup.

## 6. Publish the branch and PR

After the scope, contribution requirements, and applicable local checks are
satisfied according to sections 1–5, commit any final in-scope changes. Missing
translation-quality comparison evidence remains an owner-review matter, not
a publication blocker. Ensure the tree and recorded candidate SHA agree, then
publish without force:

```sh
git push --set-upstream origin "$branch"
gh pr create --repo BigDawnGhost/wenyi --base dev --head "$branch" \
  --title "$pr_title" --body-file "$pr_body_file"
```

Use an explicit Conventional Commit-style title and a reviewed body file
containing the change summary, verification evidence, translation-quality
evaluation status and available comparisons when relevant, risks, and
intentionally retained local files. Exclude secrets and private content.
If retrying after a partial failure, discover and reuse the PR for this exact
branch and base instead of creating a duplicate.

Source: `AGENTS.md` Git/delivery rules. The PR targeting `dev` triggers
`.github/workflows/tests.yml`; pushing an ordinary feature branch alone does
not trigger that workflow's restricted `push` event.

## 7. Require complete CI and applicable review

Before merging, explicitly verify all required checks have passed on the exact
changes being landed. Starting checks, an old successful run, or an empty list
of server-required checks is insufficient.

- Require the full Ruff and Pyright zero-error, zero-warning gate from section 5
  on the final candidate, including any conflict resolutions or review fixes.
- Require the complete current `Tests` workflow: Python 3.10, Python 3.12, and
  web jobs. Check job and step results, not only the workflow badge.
- Both Python jobs provide PostgreSQL 16 and Redis 7 and run the entire pytest
  suite. Require the applicable integration tests to execute, not be skipped
  because service variables or dependencies are missing.
- The Python 3.12 job also supplies PDF regression dependencies and builds the
  packages with the resource check. The web job runs typecheck, build, and the
  full Chromium Playwright suite with Node 22 / pnpm 9.
- Require `Build executables` when its current PR path conditions apply.
  `.github/workflows/build.yml` currently triggers on a PR to `dev` changing
  that workflow file, as well as manual dispatch or a published release.
  Do not create a release, publish packages, or dispatch release work merely to
  land a change. Its conditional release-upload job is not a normal PR gate.
- Satisfy any additional applicable remote checks, reviews, and conversation
  rules. Address blocking review findings; do not manufacture approvals or use
  administrator bypass. Optional, deliberately inapplicable test skips must be
  explained separately from missing required coverage.

Useful non-interactive inspections:

```sh
gh pr view "$pr" --repo BigDawnGhost/wenyi \
  --json url,state,baseRefName,baseRefOid,headRefName,headRefOid,reviewDecision,mergeable,mergeStateStatus,statusCheckRollup
gh pr checks "$pr" --repo BigDawnGhost/wenyi \
  --json name,state,bucket,workflow,link
gh run list --repo BigDawnGhost/wenyi --branch "$branch" \
  --event pull_request --workflow tests.yml \
  --json databaseId,headSha,status,conclusion,url
gh run view "$run_id" --repo BigDawnGhost/wenyi \
  --json headSha,status,conclusion,jobs,url
```

Match the PR head and workflow run to the recorded candidate. When checks test
GitHub's synthetic merge commit, establish its association with the current
head and integrated base. Never accept a result for an earlier revision.
Use bounded waits if watching checks; pending or timed-out waits remain
unfinished. Failed, cancelled, missing, or unverifiable required checks block
landing. If fixes are necessary, add reviewed commits, push normally, and
repeat all invalidated verification and required CI.

Fetch and recheck `dev` before merging. If it advanced beyond the integrated
base, merge the new base into the task branch, resolve conflicts according to
the agreed preference, push, and verify the resulting candidate again.

## 8. Merge and verify the destination

After all gates pass, recheck the PR base, exact head, remote rules, and merge
method availability. Prefer a merge commit, consistent with current practice:

```sh
gh pr merge "$pr" --repo BigDawnGhost/wenyi \
  --merge --match-head-commit "$verified_head"
```

Do not use `--admin`, force-push, bypass a required queue, enable early
auto-merge as a substitute for checking results, or delete branches. If the
destination now forbids merge commits or requires a different process, stop
and explain the changed requirement instead of silently choosing a method.
If a required queue accepts the PR, queued is not merged: verify its checks and
eventual merged state before declaring completion.

Finally:

```sh
gh pr view "$pr" --repo BigDawnGhost/wenyi \
  --json url,state,baseRefName,headRefOid,mergeCommit,mergedAt
git fetch origin dev
git merge-base --is-ancestor "$merge_sha" origin/dev
git --no-optional-locks status --short
```

Require `state == MERGED`, base `dev`, the expected head, an actual merge SHA,
and that merge's presence in remote `dev`. Report the PR, branch, merge commit,
verified CI, cleanup performed, retained files, and any remaining limitations.
If any blocker prevented merging, explicitly report **not landed**, the
precise blocker, and what is needed next. Do not reset or switch the user's
working tree, erase local data, or clean up branches as an unrequested epilogue.
