BEGIN TRANSACTION;
CREATE TABLE articles (
    id              TEXT PRIMARY KEY,       -- url_canonical 의 sha1
    url             TEXT NOT NULL,
    url_canonical   TEXT NOT NULL UNIQUE,   -- 정규화 후. 중복 수집 방지의 핵심
    domain          TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    lead            TEXT NOT NULL DEFAULT '',
    language        TEXT NOT NULL DEFAULT '',
    published_at    TEXT,                   -- ISO8601 UTC
    collected_at    TEXT NOT NULL,
    country_fips    TEXT NOT NULL DEFAULT '',
    source_kind     TEXT NOT NULL DEFAULT 'gdelt',  -- gdelt | rss
    tone            REAL,                   -- GDELT가 준 문서 톤 (있으면)
    blob_key        TEXT,                   -- R2 객체 키 (하루 1파일)
    blob_offset     INTEGER,                -- 그 파일 안에서의 줄 번호
    event_id        TEXT,                   -- step4가 채운다
    status          TEXT NOT NULL DEFAULT 'collected'
                                            -- collected | filtered | embedded | clustered
);
CREATE TABLE briefings (
    date        TEXT PRIMARY KEY,
    headline    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',   -- JSON: briefing_schema
    raw_response TEXT NOT NULL DEFAULT '',
    schema_ok   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE collect_plan (
    date        TEXT NOT NULL,
    fips        TEXT NOT NULL,
    quota       INTEGER NOT NULL,       -- 배분된 할당량
    collected   INTEGER NOT NULL DEFAULT 0,  -- 실제 수집량
    queries     INTEGER NOT NULL DEFAULT 0,  -- 쪼갠 쿼리 수
    PRIMARY KEY (date, fips)
);
CREATE TABLE country_weights (
    date                TEXT NOT NULL,          -- YYYY-MM-DD
    fips                TEXT NOT NULL,          -- GDELT용 FIPS 10-4
    iso2                TEXT NOT NULL,          -- 그 외 전부
    attention           REAL NOT NULL DEFAULT 0,-- 0~1 점유율 정규화
    surge               REAL NOT NULL DEFAULT 0,-- 0~1 정규화된 급등도
    surge_raw           REAL NOT NULL DEFAULT 0,-- 어제 ÷ 7일 평균 (원시 배율)
    weight              REAL NOT NULL DEFAULT 0,-- 최종 가중치
    provider_breakdown  TEXT NOT NULL DEFAULT '{}',  -- JSON: provider별 기여
    PRIMARY KEY (date, fips)
);
CREATE TABLE embeddings (
    article_id  TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    quantize    TEXT NOT NULL DEFAULT 'int8',
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE events (
    id                  TEXT PRIMARY KEY,       -- evt_YYYYMMDD_NNNN
    date                TEXT NOT NULL,
    primary_country     TEXT NOT NULL,          -- FIPS. 소속 기사 최빈값
    article_count       INTEGER NOT NULL DEFAULT 0,
    source_count        INTEGER NOT NULL DEFAULT 0,  -- 서로 다른 매체 수
    representatives     TEXT NOT NULL DEFAULT '[]',  -- JSON: 대표 기사 id 배열
    headline            TEXT NOT NULL DEFAULT '',
    summary             TEXT NOT NULL DEFAULT '',
    category            TEXT NOT NULL DEFAULT '',
    tone                REAL,                   -- -10~+10. 글로브 마커 색
    confidence          REAL,
    analysis            TEXT NOT NULL DEFAULT '',  -- JSON: 스키마 전체
    raw_response        TEXT NOT NULL DEFAULT '',  -- 검증 실패 시 원문 보존
    schema_ok           INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
                                                -- pending | submitted | analyzed | failed
    batch_id            TEXT
);
CREATE TABLE filter_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    article_id  TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL,      -- duplicate | language | domain | too_short | ...
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    step        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    batch_id    TEXT,
    status      TEXT NOT NULL DEFAULT '',   -- submitted | pending | ended | failed
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_weights_date ON country_weights(date, weight DESC);
CREATE INDEX idx_articles_country ON articles(country_fips, collected_at);
CREATE INDEX idx_articles_event ON articles(event_id);
CREATE INDEX idx_articles_status ON articles(status);
CREATE INDEX idx_filter_reason ON filter_log(date, reason);
CREATE INDEX idx_embeddings_model ON embeddings(model);
CREATE INDEX idx_events_date ON events(date, primary_country);
CREATE INDEX idx_events_status ON events(status);
CREATE INDEX idx_runs_date ON runs(date);
CREATE INDEX idx_runs_batch ON runs(batch_id);
DELETE FROM "sqlite_sequence";
COMMIT;
