-- 검색용 FTS5 인덱스. Worker 의 /api/search 가 이걸 쓴다.
-- 파이프라인 스키마(store/schema.sql)와 분리해 둔다 — D1 에만 필요하고,
-- 로컬 SQLite 에서는 FTS5 확장이 없을 수 있다.

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    id UNINDEXED,
    headline,
    summary,
    category,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- events 가 바뀌면 인덱스를 따라가게 한다.
CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events
WHEN new.schema_ok = 1
BEGIN
    INSERT INTO events_fts (id, headline, summary, category)
    VALUES (new.id, new.headline, new.summary, new.category);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_update AFTER UPDATE ON events
WHEN new.schema_ok = 1
BEGIN
    DELETE FROM events_fts WHERE id = new.id;
    INSERT INTO events_fts (id, headline, summary, category)
    VALUES (new.id, new.headline, new.summary, new.category);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events
BEGIN
    DELETE FROM events_fts WHERE id = old.id;
END;
