-- 文译 Web 模式数据库结构（PostgresStorage 使用）。
-- 幂等：CREATE ... IF NOT EXISTS。MVP 阶段以此初始化；后续可迁移到 Alembic。

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    title        TEXT,              -- 书名（原文）
    fmt          TEXT,              -- epub | text | fb2
    source_lang  TEXT,
    target_lang  TEXT,
    source_path  TEXT,              -- 上传原件在 data/ 下的相对路径
    status       TEXT DEFAULT 'created',   -- created|preparing|translating|paused|postprocessing|done|error
    strategy     JSONB,             -- 用户选择的策略定义
    meta         JSONB DEFAULT '{}'::jsonb,    -- manifest.meta（目录项等）
    context      JSONB,             -- 滚动上下文
    analysis     JSONB,             -- 风格分析 + 全书概要
    usage        JSONB,             -- LLM token 用量累计
    report       JSONB,             -- QA 报告
    book_title   TEXT,              -- 译后书名（默认=原文）
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chapters (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    title            TEXT DEFAULT '',
    title_translated TEXT,
    href             TEXT,
    template         TEXT,
    status           TEXT DEFAULT 'pending',   -- pending | done
    meta             JSONB DEFAULT '{}'::jsonb, -- review_issues / backtranslation_issues / source_digest
    PRIMARY KEY (project_id, seq)
);

CREATE TABLE IF NOT EXISTS segments (
    project_id   TEXT NOT NULL,
    chapter_seq  INTEGER NOT NULL,
    seg_seq      INTEGER NOT NULL,
    source       TEXT DEFAULT '',
    target       TEXT,
    kind         TEXT DEFAULT 'text',
    anchor       TEXT,
    cont         BOOLEAN DEFAULT FALSE,
    meta         JSONB DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, chapter_seq, seg_seq),
    FOREIGN KEY (project_id, chapter_seq) REFERENCES chapters(project_id, seq) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS glossary (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    reading      TEXT DEFAULT '',
    type         TEXT DEFAULT '术语',
    gender       TEXT DEFAULT '',
    aliases      JSONB DEFAULT '[]'::jsonb,
    first_chapter INTEGER,
    note         TEXT DEFAULT '',
    confidence   TEXT DEFAULT 'medium',
    locked       BOOLEAN DEFAULT FALSE,
    status       TEXT DEFAULT 'ok',
    updated_at   DOUBLE PRECISION,
    PRIMARY KEY (project_id, source)
);

CREATE TABLE IF NOT EXISTS term_conflicts (
    id               SERIAL PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source           TEXT NOT NULL,
    existing_target  TEXT,
    proposed_target  TEXT,
    chapter          INTEGER,
    note             TEXT,
    resolved         BOOLEAN DEFAULT FALSE,
    created_at       DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS translation_memory (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_hash  TEXT NOT NULL,
    source_text  TEXT NOT NULL,
    target_text  TEXT NOT NULL,
    chapter      INTEGER,
    updated_at   DOUBLE PRECISION,
    PRIMARY KEY (project_id, source_hash)
);

CREATE TABLE IF NOT EXISTS events (
    id          SERIAL PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    payload     JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_project_time ON events (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS exports (
    id          SERIAL PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    format      TEXT NOT NULL,
    options     JSONB DEFAULT '{}'::jsonb,
    path        TEXT,
    size        BIGINT,
    status      TEXT DEFAULT 'pending',   -- pending | done | error
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id          SERIAL PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- translation | export | prepare
    status      TEXT DEFAULT 'queued',    -- queued | running | paused | done | error
    arq_job_id  TEXT,
    error       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS strategy_templates (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    definition  JSONB NOT NULL,
    builtin     BOOLEAN DEFAULT FALSE
);

-- 术语全文检索（pg_trgm）。扩展需 superuser；失败时忽略（搜索退化为 ILIKE）。
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_glossary_source_trgm ON glossary USING gin (source gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_glossary_target_trgm ON glossary USING gin (target gin_trgm_ops);
