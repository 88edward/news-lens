/**
 * news-lens Worker — 검색과 아카이브.
 *
 * 정적 빌드는 최근 30일만 굽는다. 그보다 오래된 것과 전문 검색은 여기가 맡는다.
 *
 *   GET /api/search?q=금리&limit=20&cursor=...
 *   GET /archive/2026-08-01
 *
 * D1 무료 한도는 하루 읽기 500만 행이다. 페이지네이션을 강제하지 않으면
 * 검색 한 번이 테이블 전체를 훑어 하루치 한도를 몇 번의 요청으로 태운다.
 */

export interface Env {
  DB: D1Database;
  /** 선택: Turso 를 쓸 때의 HTTP 엔드포인트 */
  TURSO_DATABASE_URL?: string;
  TURSO_AUTH_TOKEN?: string;
}

/** 한 번에 돌려줄 수 있는 최대 행 수. 이걸 넘겨 달라고 해도 잘라낸다. */
const MAX_LIMIT = 50;
const DEFAULT_LIMIT = 20;

/** 검색어 길이 상한 — FTS 쿼리에 긴 문자열을 넣으면 비용만 커진다. */
const MAX_QUERY_CHARS = 120;

const JSON_HEADERS = {
  'content-type': 'application/json; charset=utf-8',
  'cache-control': 'public, max-age=300, s-maxage=900',
  'access-control-allow-origin': '*',
};

function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...extra },
  });
}

function clampLimit(raw: string | null): number {
  const n = Number.parseInt(raw ?? '', 10);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_LIMIT;
  return Math.min(n, MAX_LIMIT);
}

/** cursor 는 offset 을 감싼 것이다. 음수·거대값을 그대로 믿지 않는다. */
function parseCursor(raw: string | null): number {
  const n = Number.parseInt(raw ?? '', 10);
  if (!Number.isFinite(n) || n < 0) return 0;
  return Math.min(n, 5000); // 깊은 페이지는 어차피 쓸모가 없다
}

function isDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

/**
 * FTS5 쿼리 정화.
 *
 * 사용자가 넣은 `"`, `*`, `NEAR`, `-` 같은 연산자를 그대로 통과시키면
 * 구문 오류가 나거나 의도치 않게 무거운 쿼리가 된다. 토큰만 남기고
 * 각 토큰을 따옴표로 감싼 뒤 AND 로 잇는다.
 */
function sanitizeFts(raw: string): string {
  return raw
    .slice(0, MAX_QUERY_CHARS)
    .split(/[\s,.;:!?()[\]{}"'`~^*+\-/\\|<>=&%$#@]+/u)
    .filter((token) => token.length > 0)
    .slice(0, 8)
    .map((token) => `"${token.replace(/"/g, '')}"`)
    .join(' AND ');
}

async function search(env: Env, url: URL): Promise<Response> {
  const raw = (url.searchParams.get('q') ?? '').trim();
  if (raw.length < 2) {
    return json({ error: '검색어는 2자 이상이어야 한다', results: [] }, 400);
  }

  const query = sanitizeFts(raw);
  if (!query) {
    return json({ error: '검색 가능한 단어가 없다', results: [] }, 400);
  }

  const limit = clampLimit(url.searchParams.get('limit'));
  const offset = parseCursor(url.searchParams.get('cursor'));

  // LIMIT 는 항상 붙인다. 없으면 D1 읽기 한도를 한 번에 태울 수 있다.
  const stmt = env.DB.prepare(
    `SELECT e.id, e.date, e.primary_country, e.headline, e.category,
            e.tone, e.source_count
       FROM events_fts f
       JOIN events e ON e.id = f.id
      WHERE events_fts MATCH ?1
        AND e.status = 'analyzed'
      ORDER BY rank
      LIMIT ?2 OFFSET ?3`
  ).bind(query, limit + 1, offset);

  const { results } = await stmt.all();
  const rows = (results ?? []) as Record<string, unknown>[];
  const hasMore = rows.length > limit;

  return json({
    query: raw,
    results: rows.slice(0, limit),
    cursor: hasMore ? String(offset + limit) : null,
    limit,
  });
}

async function archive(env: Env, date: string, url: URL): Promise<Response> {
  if (!isDate(date)) {
    return json({ error: '날짜 형식은 YYYY-MM-DD 여야 한다' }, 400);
  }

  const limit = clampLimit(url.searchParams.get('limit'));
  const offset = parseCursor(url.searchParams.get('cursor'));

  const events = await env.DB.prepare(
    `SELECT id, primary_country, headline, category, tone, source_count
       FROM events
      WHERE date = ?1 AND status = 'analyzed'
      ORDER BY source_count DESC, id
      LIMIT ?2 OFFSET ?3`
  )
    .bind(date, limit + 1, offset)
    .all();

  const rows = (events.results ?? []) as Record<string, unknown>[];
  const hasMore = rows.length > limit;

  const briefing = offset === 0
    ? await env.DB.prepare(
        `SELECT headline, content FROM briefings WHERE date = ?1 AND schema_ok = 1`
      )
        .bind(date)
        .first()
    : null;

  return json({
    date,
    briefing,
    events: rows.slice(0, limit),
    cursor: hasMore ? String(offset + limit) : null,
    limit,
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'access-control-allow-origin': '*',
          'access-control-allow-methods': 'GET, OPTIONS',
          'access-control-max-age': '86400',
        },
      });
    }
    if (request.method !== 'GET') {
      return json({ error: 'GET 만 지원한다' }, 405);
    }

    try {
      if (url.pathname === '/api/search') {
        return await search(env, url);
      }
      const archiveMatch = url.pathname.match(/^\/archive\/([^/]+)\/?$/);
      if (archiveMatch) {
        return await archive(env, archiveMatch[1], url);
      }
      if (url.pathname === '/api/health') {
        return json({ ok: true });
      }
      return json({ error: 'not found' }, 404);
    } catch (err) {
      // 오류 본문에 SQL 이나 스택을 넣지 않는다.
      console.error('[worker]', err);
      return json({ error: '요청을 처리하지 못했다' }, 500);
    }
  },
};
