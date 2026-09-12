"""파생 데이터 저장소.

로컬/테스트는 SQLite, 운영은 Turso(libSQL). 두 경우 모두 같은 SQL을 쓴다.
TURSO_DATABASE_URL 이 있으면 Turso, 없으면 파일 SQLite 를 연다.

step 들은 이전 단계 결과를 인자로 받지 않고 여기서 읽는다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = REPO_ROOT / "news-lens.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    """얇은 래퍼. ORM을 두지 않는다 — 쿼리가 보이는 편이 낫다."""

    def __init__(self, path: str | Path | None = None, *, memory: bool = False):
        if memory:
            self.path = ":memory:"
        else:
            self.path = str(path or os.environ.get("NEWS_LENS_DB") or DEFAULT_DB_PATH)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # ── 기본 ──────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is None:
            self.commit()
        self.close()

    # ── country_weights (step0) ───────────────────────────────────────

    def save_weights(self, date: str, rows: Iterable[dict]) -> int:
        n = 0
        for row in rows:
            self.execute(
                """INSERT INTO country_weights
                   (date, fips, iso2, attention, surge, surge_raw, weight, provider_breakdown)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date, fips) DO UPDATE SET
                     attention=excluded.attention, surge=excluded.surge,
                     surge_raw=excluded.surge_raw, weight=excluded.weight,
                     provider_breakdown=excluded.provider_breakdown""",
                (
                    date,
                    row["fips"],
                    row["iso2"],
                    row.get("attention", 0.0),
                    row.get("surge", 0.0),
                    row.get("surge_raw", 0.0),
                    row.get("weight", 0.0),
                    json.dumps(row.get("provider_breakdown", {}), ensure_ascii=False),
                ),
            )
            n += 1
        self.commit()
        return n

    def latest_weight_date(self, before: str | None = None) -> str | None:
        if before:
            row = self.one(
                "SELECT MAX(date) AS d FROM country_weights WHERE date < ?", (before,)
            )
        else:
            row = self.one("SELECT MAX(date) AS d FROM country_weights")
        return row["d"] if row and row["d"] else None

    def weights_for(self, date: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        """해당 날짜의 가중치. 날짜를 안 주면 가장 최근 것을 쓴다."""
        date = date or self.latest_weight_date()
        if not date:
            return []
        sql = "SELECT * FROM country_weights WHERE date = ? ORDER BY weight DESC"
        params: list[Any] = [date]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.query(sql, params)

    # ── collect_plan (step1) ──────────────────────────────────────────

    def save_plan(self, date: str, plan: dict[str, int]) -> None:
        for fips, quota in plan.items():
            self.execute(
                """INSERT INTO collect_plan (date, fips, quota) VALUES (?, ?, ?)
                   ON CONFLICT(date, fips) DO UPDATE SET quota=excluded.quota""",
                (date, fips, int(quota)),
            )
        self.commit()

    def bump_collected(self, date: str, fips: str, n: int, queries: int = 0) -> None:
        self.execute(
            """UPDATE collect_plan SET collected = collected + ?, queries = queries + ?
               WHERE date = ? AND fips = ?""",
            (n, queries, date, fips),
        )

    def plan_for(self, date: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM collect_plan WHERE date = ? ORDER BY quota DESC", (date,)
        )

    # ── articles ──────────────────────────────────────────────────────

    def article_exists(self, url_canonical: str) -> bool:
        return (
            self.one(
                "SELECT 1 FROM articles WHERE url_canonical = ?", (url_canonical,)
            )
            is not None
        )

    def known_urls(self) -> set[str]:
        return {r["url_canonical"] for r in self.query("SELECT url_canonical FROM articles")}

    def insert_article(self, row: dict) -> bool:
        """이미 있는 url이면 False. 다시 받지 않는다."""
        cur = self.execute(
            """INSERT OR IGNORE INTO articles
               (id, url, url_canonical, domain, title, lead, language, published_at,
                collected_at, country_fips, source_kind, tone, blob_key, blob_offset, status)
               VALUES (:id, :url, :url_canonical, :domain, :title, :lead, :language,
                       :published_at, :collected_at, :country_fips, :source_kind, :tone,
                       :blob_key, :blob_offset, :status)""",
            {
                "status": "collected",
                "tone": None,
                "blob_key": None,
                "blob_offset": None,
                "published_at": None,
                **row,
            },
        )
        return cur.rowcount > 0

    def articles_by_status(self, status: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM articles WHERE status = ? ORDER BY collected_at"
        params: list[Any] = [status]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.query(sql, params)

    def set_article_status(self, article_id: str, status: str) -> None:
        self.execute("UPDATE articles SET status = ? WHERE id = ?", (status, article_id))

    def log_filtered(self, date: str, article_id: str, url: str, reason: str, detail: str = "") -> None:
        self.execute(
            "INSERT INTO filter_log (date, article_id, url, reason, detail) VALUES (?, ?, ?, ?, ?)",
            (date, article_id, url, reason, detail),
        )

    def filter_summary(self, date: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT reason, COUNT(*) AS n FROM filter_log WHERE date = ? GROUP BY reason ORDER BY n DESC",
            (date,),
        )

    # ── embeddings ────────────────────────────────────────────────────

    def save_embedding(
        self, article_id: str, model: str, dimensions: int, quantize: str, vector: bytes
    ) -> None:
        self.execute(
            """INSERT INTO embeddings (article_id, model, dimensions, quantize, vector, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET
                 model=excluded.model, dimensions=excluded.dimensions,
                 quantize=excluded.quantize, vector=excluded.vector,
                 created_at=excluded.created_at""",
            (article_id, model, dimensions, quantize, vector, utcnow()),
        )

    def embedded_ids(self, model: str) -> set[str]:
        """같은 모델로 이미 임베딩된 기사. 모델이 다르면 다시 만들어야 한다."""
        return {
            r["article_id"]
            for r in self.query("SELECT article_id FROM embeddings WHERE model = ?", (model,))
        }

    def embeddings_for(self, model: str, ids: Sequence[str] | None = None) -> list[sqlite3.Row]:
        if ids:
            marks = ",".join("?" * len(ids))
            return self.query(
                f"SELECT * FROM embeddings WHERE model = ? AND article_id IN ({marks})",
                [model, *ids],
            )
        return self.query("SELECT * FROM embeddings WHERE model = ?", (model,))

    # ── events ────────────────────────────────────────────────────────

    def insert_event(self, row: dict) -> None:
        self.execute(
            """INSERT INTO events
               (id, date, primary_country, article_count, source_count, representatives, status)
               VALUES (:id, :date, :primary_country, :article_count, :source_count,
                       :representatives, 'pending')
               ON CONFLICT(id) DO UPDATE SET
                 primary_country=excluded.primary_country,
                 article_count=excluded.article_count,
                 source_count=excluded.source_count,
                 representatives=excluded.representatives""",
            row,
        )

    def next_event_index(self, date: str) -> int:
        """그날 이미 쓴 사건 번호 다음. 재실행이 기존 사건을 덮어쓰지 않게 한다."""
        row = self.one("SELECT COUNT(*) AS n FROM events WHERE date = ?", (date,))
        return int(row["n"] or 0) + 1

    def reset_clustering(self, date: str) -> int:
        """그날 사건을 전부 지우고 기사를 embedded 로 되돌린다 (--recluster)."""
        ids = [r["id"] for r in self.query("SELECT id FROM events WHERE date = ?", (date,))]
        if ids:
            marks = ",".join("?" * len(ids))
            self.execute(
                f"UPDATE articles SET event_id = NULL, status = 'embedded' "
                f"WHERE event_id IN ({marks})",
                ids,
            )
            self.execute(f"DELETE FROM events WHERE id IN ({marks})", ids)
            self.commit()
        return len(ids)

    def assign_event(self, article_id: str, event_id: str) -> None:
        self.execute(
            "UPDATE articles SET event_id = ?, status = 'clustered' WHERE id = ?",
            (event_id, article_id),
        )

    def events_for(self, date: str, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.query(
                "SELECT * FROM events WHERE date = ? AND status = ? ORDER BY source_count DESC",
                (date, status),
            )
        return self.query(
            "SELECT * FROM events WHERE date = ? ORDER BY source_count DESC", (date,)
        )

    def articles_of_event(self, event_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM articles WHERE event_id = ? ORDER BY published_at", (event_id,)
        )

    def mark_events_submitted(self, event_ids: Sequence[str], batch_id: str) -> None:
        for eid in event_ids:
            self.execute(
                "UPDATE events SET status = 'submitted', batch_id = ? WHERE id = ?",
                (batch_id, eid),
            )
        self.commit()

    def save_event_analysis(
        self,
        event_id: str,
        *,
        analysis: dict | None,
        raw: str,
        schema_ok: bool,
    ) -> None:
        a = analysis or {}
        self.execute(
            """UPDATE events SET
                 headline = ?, summary = ?, category = ?, tone = ?, confidence = ?,
                 analysis = ?, raw_response = ?, schema_ok = ?,
                 status = ?
               WHERE id = ?""",
            (
                a.get("한줄요약", "") or a.get("headline", ""),
                a.get("배경", "") or a.get("summary", ""),
                a.get("카테고리", "") or a.get("category", ""),
                a.get("논조점수", a.get("tone")),
                a.get("신뢰도", a.get("confidence")),
                json.dumps(a, ensure_ascii=False) if a else "",
                raw,
                1 if schema_ok else 0,
                "analyzed" if schema_ok else "failed",
                event_id,
            ),
        )

    # ── briefings ─────────────────────────────────────────────────────

    def save_briefing(self, date: str, headline: str, content: dict, raw: str, schema_ok: bool) -> None:
        self.execute(
            """INSERT INTO briefings (date, headline, content, raw_response, schema_ok, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 headline=excluded.headline, content=excluded.content,
                 raw_response=excluded.raw_response, schema_ok=excluded.schema_ok,
                 created_at=excluded.created_at""",
            (date, headline, json.dumps(content, ensure_ascii=False), raw, 1 if schema_ok else 0, utcnow()),
        )
        self.commit()

    def briefing_for(self, date: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM briefings WHERE date = ?", (date,))

    # ── runs / 비용 ───────────────────────────────────────────────────

    def record_run(
        self,
        *,
        day: str,
        role: str = "",
        model: str = "",
        step: str = "",
        batch_id: str | None = None,
        status: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        note: str = "",
    ) -> int:
        cur = self.execute(
            """INSERT INTO runs
               (date, step, role, model, batch_id, status, input_tokens, output_tokens,
                cost_usd, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (day, step, role, model, batch_id, status, input_tokens, output_tokens,
             cost_usd, note, utcnow()),
        )
        self.commit()
        return int(cur.lastrowid or 0)

    def spent_today(self, day: str) -> float:
        row = self.one("SELECT SUM(cost_usd) AS s FROM runs WHERE date = ?", (day,))
        return float(row["s"] or 0.0) if row else 0.0

    def open_batches(self) -> list[sqlite3.Row]:
        """아직 수거하지 않은 batch. step6이 폴링한다."""
        return self.query(
            """SELECT * FROM runs
               WHERE batch_id IS NOT NULL AND status IN ('submitted', 'pending')
               ORDER BY created_at""",
        )

    def set_batch_status(self, batch_id: str, status: str) -> None:
        self.execute("UPDATE runs SET status = ? WHERE batch_id = ?", (status, batch_id))
        self.commit()


def connect(path: str | Path | None = None, *, memory: bool = False) -> Database:
    return Database(path, memory=memory)
