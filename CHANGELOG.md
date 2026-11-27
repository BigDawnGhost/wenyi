# Changelog

All notable changes to Wenyi are summarized here, newest release first. Entries
group merged Conventional Commits; keep the Unreleased section current with

    uv run --no-sync python scripts/release/update_changelog.py

When tagging a release, convert that section with ``--version <tag>`` and draft
the bilingual GitHub Release description with the release-notes skill.
Historical releases remain available in the repository's GitHub Releases pages.

## Unreleased
_No changes recorded._

## v1.8.0 - 2026-11-27
- Features
  - desktop: add persistent interface zoom with a keyboard shortcut to reset it
  - desktop: offer explicit offline recovery from the verified snapshot for a pending upgrade when the catalog format is refused
  - cli: point unexpected errors at run state
  - desktop: surface diagnostics without a receiver
  - cli: make log rotation configurable and tag runs
  - cli: publish to a numbered filename if the output already exists and overwrite was not requested
  - cli: report per-format export outcomes
  - core: make wenyi-locks fallback observable and controllable
  - desktop: verify backend version handshake on startup
  - cli: pass structured progress payloads through machine output
  - pdf: retry transient health and fillback failures without replaying uncertain extraction requests
  - epub: use saved translations for book titles, subject tags, and matching page titles in single-language and bilingual exports
  - settings: autosave ordinary settings and test unsaved provider edits in Web and Desktop
  - epub: report unresolved internal references
  - api: version the postgres schema migrations
  - backend: sign queued task payloads
  - llm: inherit omitted project route and budget fields; allow explicit clearing of thinking overrides and fallbacks
  - llm: configure the prompt-byte token estimate divisor, now defaulting to 2.0
  - llm: record max_tokens in usage samples
- Bug fixes
  - workflows: pause queued work immediately and stop running writers before resuming from committed checkpoints
  - storage: keep active temporary files and published artifacts out of initialization cleanup
  - storage: skip damaged JSONL records without losing later checkpoints; explicitly sync glossary checkpoints
  - pdf: publish the BabelDOC extraction cache atomically
  - ui: keep reading and error text selectable without making interface labels selectable
  - core: support long paths on windows
  - assemble: stage docx media through the tmp convention
  - ingest: pass fb2 image alt text through to epub
  - core: default prompt rendering to english
  - ui: give tabs the standard aria pattern
  - ui: raise light theme contrast to wcag aa
  - ui: wire aria attributes into form validation errors
  - desktop: fall back to english for missing translations
  - cli: surface failure reasons in machine receipts
  - cli: fail fast on missing explicit config
  - desktop: guide users away from legacy data layouts
  - desktop: prune upgrade backups with retention limits
  - desktop: preserve keyboard focus when the BabelDOC plugin card loads
  - export: explain orphaned continuation failures
  - storage: validate file artifact keys portably
  - docx: restore source images in exports
  - ingest: report the failing MinerU stage without exposing signed URLs or credentials
  - epub: tolerate continuation-first footnote slices
  - api: fail closed on token requirement parsing
  - epub: rebuild the table of contents hierarchy
  - backend: compare ws tokens as bytes
  - storage: shorten new CLI book paths and recognize verified legacy state directories
  - api: write job admission atomically
  - export: do not reuse a running export that may have captured an older chapter snapshot
  - api: distinguish export editions and preserve successful publications when cleanup must retry
  - native: convert rust panics into python runtime errors
  - api: stop session locks from pinning shared pool connections
  - backend: land failed terminal state on native panics
  - storage: clear stale initialization markers at startup
  - backend: judge live statistics freshness by cache TTL
  - api: roll nested state-lock failures back to a savepoint
  - llm: reject unknown operation flags at registration
  - storage: make file lock reentrancy explicit and waits observable
  - review: persist review usage at chunk checkpoints
  - storage: fail fast on corrupt sqlite state database
  - agents: let budget and config errors escape the fallback path
  - storage: publish chapter and manifest as one atomic commit
  - llm: release failed-request budget and flush usage per batch
- Performance improvements
  - ingest: stream MinerU uploads and downloads and open one result archive at a time
  - storage: write staged chapters in batches
  - export: cache manifests per view session
  - ingest: stream babeldoc paragraphs into chapters
  - export: load chapter views on demand
- Documentation
  - document the bridge retry, fillback, and ledger contracts
  - document the file lock environment switches
  - cli: sync config creation guidance in guide
- Internal changes
  - toolchain: use Node 26 and pin pnpm 9.15.9
  - packaging: enforce matching CLI and Core package versions
  - align core tests with explicit config handling
  - skip workflow checks without git for windows bash
  - desktop: accept task payload kwargs in runtime fakes
  - sync changelog
  - adapt path assertions for long path prefixes
  - add changelog sync tooling
  - run the hardened suite on main pushes
  - ci: pin third-party actions to commit shas
  - add just recipes for common commands
  - ci: add dependabot and pr template
  - fail loud on missing fake llm branches
  - treat core warnings as errors
  - add context to bare assertions
  - unify assertion dialects
  - share one metered fake client
  - distribute pytest workers by book group
  - split the orchestrator suite by scenario
  - replace timed sleeps with event-driven waits
  - frontend: generate third-party notices at build time
  - split the newfeatures misc suite by domain
  - lock workflow key jobs behind a meta test
  - rust: declare and enforce the native msrv
  - extend type checking and report test coverage
  - clean up root temp leftovers and ignore them
  - cache rust builds in release pipelines
  - desktop: make silent exception handling observable
  - add a windows smoke job for python tests
  - deps: mirror the CI node and pnpm versions in engines
  - deps: pin ruff to a single version source
  - backend: fail fast with guidance when test deps are missing
  - core: fail fast when the rust toolchain is missing
  - desktop: register ipc error codes in shared schema
  - backend: share one credential redactor exit
  - glossary: derive term store paths from the slug chain
  - native: add feature-gated panic injection probe
  - book-understanding: shorten digest parametrize ids for Windows env limit
  - routing: read manifest with explicit utf-8 encoding
