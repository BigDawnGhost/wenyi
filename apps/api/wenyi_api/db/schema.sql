-- Fresh-deployment PostgreSQL schema. No legacy-state migrations are provided.
CREATE TABLE IF NOT EXISTS application_settings (
    id INTEGER PRIMARY KEY CHECK (id=1),
    document JSONB NOT NULL,
    default_template TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT,
    fmt TEXT,
    source_lang TEXT,
    target_lang TEXT,
    source_path TEXT,
    source_sha256 TEXT,
    source_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    initialized BOOLEAN NOT NULL DEFAULT FALSE,
    initialization_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    error TEXT,
    strategy JSONB,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    context JSONB,
    annotation_contexts JSONB,
    analysis JSONB,
    usage JSONB,
    report JSONB,
    book_title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (initialization_sha256 IS NULL OR initialization_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE IF NOT EXISTS chapters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    title_translated TEXT,
    href TEXT,
    template TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    review_status TEXT NOT NULL DEFAULT 'pending',
    manifest_entry JSONB NOT NULL DEFAULT '{}'::jsonb,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, seq)
);
CREATE TABLE IF NOT EXISTS segments (
    project_id TEXT NOT NULL,
    chapter_seq INTEGER NOT NULL,
    seg_seq INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    target TEXT,
    target_before_polish TEXT,
    kind TEXT NOT NULL DEFAULT 'text',
    anchor TEXT,
    cont BOOLEAN NOT NULL DEFAULT FALSE,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    resource_href TEXT,
    PRIMARY KEY (project_id, chapter_seq, seg_seq),
    FOREIGN KEY (project_id, chapter_seq) REFERENCES chapters(project_id, seq) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS segment_revisions (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    chapter_seq INTEGER NOT NULL,
    seg_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    previous_target TEXT,
    new_target TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, chapter_seq) REFERENCES chapters(project_id, seq) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_segment_revisions_paragraph
    ON segment_revisions (project_id, chapter_seq, seg_seq, id DESC);
CREATE TABLE IF NOT EXISTS glossary (
    insertion_id BIGSERIAL NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    reading TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'term',
    gender TEXT NOT NULL DEFAULT '',
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    first_chapter INTEGER,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    updated_at DOUBLE PRECISION,
    PRIMARY KEY (project_id, source)
);
CREATE INDEX IF NOT EXISTS idx_glossary_insertion ON glossary (project_id, insertion_id);
CREATE TABLE IF NOT EXISTS term_conflicts (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    existing_target TEXT,
    proposed_target TEXT,
    chapter INTEGER,
    note TEXT,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION
);
-- Durable review metadata, chunks, checkpoints, evidence and publication journals;
-- subtitle manifests/cue maps/batches; usage journals and timing ledgers.
-- Keys are normalized relative artifact names, never filesystem destinations.
CREATE TABLE IF NOT EXISTS artifacts (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, key)
);
CREATE TABLE IF NOT EXISTS artifact_events (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_artifact_events_key ON artifact_events (project_id, key, id);
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_project_time ON events (project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_manual_paragraph
    ON events (project_id, (payload->>'chapter'), (payload->>'index'), id DESC)
    WHERE type='manual_translation_edited';
CREATE TABLE IF NOT EXISTS exports (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    options JSONB NOT NULL DEFAULT '{}'::jsonb,
    path TEXT,
    size BIGINT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
ALTER TABLE exports ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    arq_job_id TEXT,
    run_id TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_project_time ON jobs (project_id, id DESC);
CREATE TABLE IF NOT EXISTS strategy_templates (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    definition JSONB NOT NULL,
    builtin BOOLEAN NOT NULL DEFAULT FALSE
);
-- pg_trgm is a trusted extension on supported PostgreSQL releases.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_glossary_source_trgm ON glossary USING gin (source gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_glossary_target_trgm ON glossary USING gin (target gin_trgm_ops);
