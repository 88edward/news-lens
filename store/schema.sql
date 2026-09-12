-- news-lens 파생 데이터 스키마.
-- 원문 HTML은 여기 들어오지 않는다 — R2에 gzip JSONL로 간다. 여기는 위치만 갖는다.
-- SQLite / Turso(libSQL) / D1 모두에서 동작하는 문법만 쓴다.

-- ── 국가 관심도 가중치 (step0) ──────────────────────────────────────
-- 날짜별로 쌓는다. 덮어쓰면 가중치 변화 추이를 볼 수 없다.
CREATE TABLE IF NOT EXISTS country_weights (
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
CREATE INDEX IF NOT EXISTS idx_weights_date ON country_weights(date, weight DESC);

-- ── 수집 계획 (step1-a) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collect_plan (
    date        TEXT NOT NULL,
    fips        TEXT NOT NULL,
    quota       INTEGER NOT NULL,       -- 배분된 할당량
    collected   INTEGER NOT NULL DEFAULT 0,  -- 실제 수집량
    queries     INTEGER NOT NULL DEFAULT 0,  -- 쪼갠 쿼리 수
    PRIMARY KEY (date, fips)
);

-- ── 기사 (step1-c) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS articles (
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
CREATE INDEX IF NOT EXISTS idx_articles_country ON articles(country_fips, collected_at);
CREATE INDEX IF NOT EXISTS idx_articles_event ON articles(event_id);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

-- ── 필터 로그 (step2) ───────────────────────────────────────────────
-- 걸러낸 이유를 남기지 않으면 임계값을 손댈 근거가 영원히 없다.
CREATE TABLE IF NOT EXISTS filter_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    article_id  TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL,      -- duplicate | language | domain | too_short | ...
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_filter_reason ON filter_log(date, reason);

-- ── 임베딩 (step3) ──────────────────────────────────────────────────
-- model 컬럼이 없으면 모델 교체 후 옛 벡터와 거리 계산을 해버린다.
CREATE TABLE IF NOT EXISTS embeddings (
    article_id  TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    quantize    TEXT NOT NULL DEFAULT 'int8',
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);

-- ── 사건 (step4 생성, step6이 분석 결과를 채움) ─────────────────────
CREATE TABLE IF NOT EXISTS events (
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
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date, primary_country);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

-- ── 일일 브리핑 (step7) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS briefings (
    date        TEXT PRIMARY KEY,
    headline    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',   -- JSON: briefing_schema
    raw_response TEXT NOT NULL DEFAULT '',
    schema_ok   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- ── 실행 기록 / 비용 원장 ───────────────────────────────────────────
-- step5와 step6은 다른 프로세스다. 여기를 안 읽으면 상한이 실행마다 초기화된다.
CREATE TABLE IF NOT EXISTS runs (
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
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(date);
CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id);
