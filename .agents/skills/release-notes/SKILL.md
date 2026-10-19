---
name: release-notes
description: >-
  Draft copy-ready English and Chinese Wenyi release notes from the changes since
  the previous stable version. Use when preparing GitHub Release descriptions,
  summarizing a release range, or standardizing bilingual changelog formatting.
---

# Draft Wenyi release notes

Generate a factual, user-facing GitHub Release description, not a commit dump.
Default to equivalent English and Simplified Chinese sections that Wenyi Desktop
can select by interface language. Deliver the Markdown in the conversation for
copying; do not create a changelog file unless requested.

This skill only drafts text. It does not authorize commits, tags, merges, pushes,
release creation or editing, workflow dispatch, package publication, or changes
to application code. Preserve existing changes and local data.

## 1. Establish the comparison range

- Honor an explicit base version, target tag/commit/branch, component scope, and
  requested language. Otherwise use committed `HEAD` as the target, covering all
  affected Wenyi components. Do not silently substitute `main`, `dev`, a remote
  branch, or the highest version visible anywhere in the repository.
- Inspect Git status, target identity, release tags and their ancestry. Resolve
  the base and target to commit SHAs so the comparison is reproducible.
- Select the nearest preceding stable version on the target's release history.
  Stable tags are numeric releases with one to three components, optionally
  prefixed with `v`, such as `v2`, `v1.3`, or `v1.2.3`; exclude prerelease,
  development and local versions. Check ancestry, not just version/date sorting.
- If the target is itself a stable release tag, compare against the stable
  release before it. Exclude every tag alias pointing at the target commit;
  comparing the release to itself must not produce an empty changelog.
- Prefer the target's first-parent release line when finding the previous
  release, but inspect all commits in the resulting range for changes. Do not
  omit feature-branch commits by using a first-parent-only change inventory.
- When GitHub publication status is available, distinguish published stable
  releases from drafts/prereleases. Do not use GitHub's global "latest" release
  if it is outside the target's ancestry. Without publication evidence, state
  outside the copyable body that the baseline comes from local Git tags.
- Exclude staged, unstaged and untracked changes by default. Mention their
  exclusion outside the release body. Include relevant working-tree changes
  only when explicitly requested; call that output an unpublished working-tree
  draft and keep unrelated changes/private data out.
- If tags/history are missing, the explicit base is not an ancestor, or several
  release lines make the previous version ambiguous, ask for the missing base
  or target. Do not invent a baseline, release number, date, or publication state.
  If the verified range has no effective changes, say so rather than inventing
  release highlights.

Useful read-only inspections after resolving `$base` and `$target`:

```sh
git --no-optional-locks status --short
git remote -v
git rev-parse --verify "${base}^{commit}"
git rev-parse --verify "${target}^{commit}"
git merge-base --is-ancestor "$base" "$target"
git log --reverse --no-merges --format='%h %s' "$base..$target"
git diff --stat "$base" "$target"
git diff --name-status "$base" "$target"
```

These commands are starting points, not sufficient evidence for every claim.
Read applicable repository instructions and narrow subsequent diffs to the
components whose behavior needs confirmation.

## 2. Turn evidence into release highlights

- Cross-check commit/PR descriptions against the net diff, relevant code,
  tests and current documentation. A reverted change is not a release feature.
  Deduplicate repeated fixes, follow-up commits and merge descriptions.
- Describe the user-visible result and affected platform or workflow. Keep
  Desktop-only capabilities separate from Web, CLI and shared Core behavior.
  State constraints such as supported installers or manual setup when relevant.
- Group related changes into meaningful highlights instead of listing every
  file, commit, dependency bump, test addition or internal refactor. Include
  development/documentation changes only when useful to the intended audience.
- Identify evidence-backed upgrade actions: configuration changes, migration
  requirements, cache invalidation, credential handling, compatibility changes,
  extra model calls/cost, or changed defaults. Preserve important caveats.
- Do not claim universal compatibility, performance gains, translation-quality
  improvements, data preservation, security guarantees, CI success, signing or
  platform testing without supporting evidence. Existing tests are not proof
  that they were run. Drafting notes does not require running the full test suite
  or contacting models, parser services, or private project data.
- Use real PR numbers, issue links and contributor credits only when verified.
  Do not infer authorship or credit from a display name in the conversation.
- Keep the two languages equivalent in capabilities, scope, warnings and upgrade
  instructions; preserve identifiers, command names and configuration keys.

## 3. Use the Desktop-compatible Markdown contract

Sources of truth:

- `docs/desktop.md`, section "Application updates".
- `docs/zh/desktop.md`, section "应用更新".
- `apps/desktop/frontend/src/localizedReleaseNotes.ts`, when present, and its affected
  regressions in `apps/desktop/frontend/tests/desktop-updates.spec.ts`.

Read the current sources before drafting; do not assume a historical template
still matches the application's parser.

Required structure:

1. Start directly with `## English`; do not add a version title or bilingual
   preface above it. GitHub's Release title is separate.
2. Write a short English overview, then applicable categories.
3. Insert one standalone `---` between the two language sections.
4. Use exactly `## 中文`, followed by an equivalent Chinese overview and categories.
5. Use `###` for category headings and `####` for optional deeper subheadings.
   Do not put other level-one/two headings or horizontal dividers inside a
   language section: they end that section in Desktop.
6. Optionally append a shared comparison link after a final standalone `---`.
   Derive its public repository URL from a verified source remote, never the
   `local` backlink. Pin the target to an existing release tag or resolved commit
   SHA, not a guessed future tag or moving `main`/`dev` ref. Omit the link if its
   repository or range cannot be established.

Use these categories in this order, omitting every empty category:

| English | Simplified Chinese |
| --- | --- |
| New features | 新增功能 |
| Improvements | 改进 |
| Fixes | 问题修复 |
| Development and documentation | 开发与文档 |
| Upgrade notes | 升级说明 |

Use concise bullets, normally one or two sentences each. Prefer
`- **Short label:** Result and important scope.` and its Chinese equivalent
`- **简短标签：** 行为变化及重要适用范围。`.
Use backticks for commands, paths, configuration keys and code identifiers.
Prefer portable Markdown without raw HTML, images or task checkboxes.
Place language-specific links and warnings inside their language section;
only the optional comparison footer is shared.

## 4. Deliver one copyable release body

- Before the copyable body, briefly state the verified base and target, whether
  this is a released version or an unreleased draft, and any excluded local
  changes or evidence limitations. Keep this process context in the user's
  conversation language, outside the Release description.
- Return the complete body in one fenced `markdown` code block, using a longer
  outer fence if the body contains code fences. It must be ready to paste, with
  no placeholders, editing instructions, analysis, test transcripts, Delta
  worktree links, or unsupported assertions.
- Do not insert stale scope disclaimers such as "a new release tag has not yet
  been published" into the body of an existing release. Do not print alternate
  versions of the same body unless requested.
- If only one output language was explicitly requested, retain its exact
  language heading and omit the other section and inter-language divider.
- Check the final Markdown: language headings are exact and unique, categories
  match across languages, no internal divider truncates a section, the overview
  and bullets match the verified range, and any comparison link is real.

Formatting skeleton only; replace all placeholders and omit empty categories:

````markdown
## English

<Brief summary of the release's main user-facing changes.>

### New features

- **<Feature>:** <What users can now do, including platform scope.>

### Fixes

- **<Fix>:** <The corrected behavior and affected workflow.>

### Upgrade notes

- <Only upgrade actions or caveats supported by the changes.>

---

## 中文

<与英文等价的简短更新概述。>

### 新增功能

- **<功能>：** <新增能力及平台适用范围。>

### 问题修复

- **<修复>：** <修正后的行为及受影响的流程。>

### 升级说明

- <仅列出有依据的升级操作或注意事项。>
````
