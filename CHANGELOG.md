# Changelog

All notable changes to Wenyi are summarized here, newest release first. Entries
group merged Conventional Commits; keep the Unreleased section current with

    uv run --no-sync python scripts/release/update_changelog.py

When tagging a release, convert that section with ``--version <tag>`` and draft
the bilingual GitHub Release description with the release-notes skill.
Historical releases remain available in the repository's GitHub Releases pages.

## Unreleased
- Features
  - cli: point unexpected errors at run state
  - desktop: surface diagnostics without a receiver
  - cli: make log rotation configurable and tag runs
  - cli: avoid silent export overwrites
  - cli: report per-format export outcomes
  - core: make wenyi-locks fallback observable and controllable
  - desktop: verify backend version handshake on startup
  - cli: pass structured progress payloads through machine output
  - pdf: fill back translations in chunks
  - pdf: retry idempotent bridge calls
  - epub: write export warnings from the assembler
  - epub: report unresolved internal references
  - api: version the postgres schema migrations
  - backend: sign queued task payloads
  - glossary: add opt-in retroactive term pass
  - llm: deep-merge layered llm settings by key
  - llm: record max_tokens in usage samples
- Bug fixes
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
  - desktop: restore upgrade backups on migration failure
  - export: explain orphaned continuation failures
  - storage: validate file artifact keys portably
  - docx: restore source images in exports
  - ingest: surface mineru failure reasons
  - epub: tolerate continuation-first footnote slices
  - api: fail closed on token requirement parsing
  - epub: rebuild the table of contents hierarchy
  - backend: compare ws tokens as bytes
  - storage: cap slug length for windows paths
  - api: write job admission atomically
  - backend: bound cooperative cancellation drain
  - glossary: shadow terms by longest span and inject per batch
  - glossary: normalize term identity for consistent matching
  - api: publish exports from a consistent database snapshot
  - native: convert rust panics into python runtime errors
  - api: stop session locks from pinning shared pool connections
  - backend: land failed terminal state on native panics
  - storage: clear stale initialization markers at startup
  - backend: judge live statistics freshness by cache TTL
  - api: roll nested state-lock failures back to a savepoint
  - llm: reject unknown operation flags at registration
  - storage: make file lock reentrancy explicit and waits observable
  - review: persist review usage at chunk checkpoints
  - translate: isolate per-chapter translation failures
  - storage: fail fast on corrupt sqlite state database
  - agents: let budget and config errors escape the fallback path
  - storage: publish chapter and manifest as one atomic commit
  - llm: release failed-request budget and flush usage per batch
- Performance improvements
  - storage: write staged chapters in batches
  - export: cache manifests per view session
  - ingest: stream babeldoc paragraphs into chapters
  - export: load chapter views on demand
- Documentation
  - document the file lock environment switches
  - cli: sync config creation guidance in guide
  - zh: mirror glossary matching updates
- Internal changes
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
  - translate: align note-loop resume test with chapter isolation
  - book-understanding: shorten digest parametrize ids for Windows env limit
  - routing: read manifest with explicit utf-8 encoding
