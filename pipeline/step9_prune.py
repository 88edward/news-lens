"""step9 — 오래된 행을 지워 DB 크기를 묶어 둔다.

DB 를 레포에 커밋하는 구성에서는 크기가 곧 레포 크기다. 방치하면 한 해 만에
클론이 불가능해진다. 그래서 매일 publish 끝에 정리한다.

무엇을 지우는가:
  - 임베딩: 클러스터링이 끝나면 쓸 데가 없다. 가장 큰 덩어리다(벡터당 768B).
  - 기사 행: retention.article_days 이후. 사건과 출처 링크는 events 에 남는다.
  - filter_log: retention.filter_log_days 이후.
  - 사건 분석 JSON 전문: retention.analysis_days 이후 비운다.
    전문은 이미 정적 페이지에 구워져 있고, 헤드라인·논조·카테고리는 남긴다.

무엇을 남기는가: events(슬림), briefings, country_weights, runs.
이게 이 서비스의 실제 산출물이고, 행당 크기가 작다.

    python -m pipeline.step9_prune --report
    python -m pipeline.step9_prune --dry-run --report
"""
from __future__ import annotations

import sys
from datetime import date as date_cls
from datetime import timedelta

import config
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, table, utc_today


def build_parser():
    p = base_parser("오래된 행을 지워 DB 크기를 묶는다")
    p.add_argument(
        "--vacuum",
        action="store_true",
        help="정리 후 VACUUM 으로 파일을 실제로 줄인다 (느리다)",
    )
    return p


def cutoff(today: str, days: int) -> str:
    return (date_cls.fromisoformat(today) - timedelta(days=days)).isoformat()


def plan(db, today: str) -> list[tuple[str, str, list]]:
    """(설명, SQL, 파라미터) 목록. dry-run 이 세어 보기만 할 수 있게 분리한다."""
    r = config.pipeline().get("retention") or {}
    jobs: list[tuple[str, str, list]] = []

    if r.get("drop_embeddings_after_cluster", True):
        jobs.append(
            (
                "클러스터링이 끝난 기사의 임베딩",
                """DELETE FROM embeddings WHERE article_id IN (
                     SELECT id FROM articles WHERE event_id IS NOT NULL
                   )""",
                [],
            )
        )

    jobs.append(
        (
            f"기사 행 ({r.get('article_days', 14)}일 이전)",
            "DELETE FROM articles WHERE collected_at < ?",
            [cutoff(today, int(r.get("article_days", 14)))],
        )
    )
    jobs.append(
        (
            f"filter_log ({r.get('filter_log_days', 14)}일 이전)",
            "DELETE FROM filter_log WHERE date < ?",
            [cutoff(today, int(r.get("filter_log_days", 14)))],
        )
    )
    jobs.append(
        (
            f"사건 분석 전문 ({r.get('analysis_days', 60)}일 이전)",
            """UPDATE events SET analysis = '', raw_response = ''
                WHERE date < ? AND analysis != ''""",
            [cutoff(today, int(r.get("analysis_days", 60)))],
        )
    )
    jobs.append(
        (
            f"가중치 이력 ({r.get('weights_history_days', 365)}일 이전)",
            "DELETE FROM country_weights WHERE date < ?",
            [cutoff(today, int(r.get("weights_history_days", 365)))],
        )
    )
    return jobs


def count_sql(sql: str) -> str:
    """DELETE/UPDATE 를 세어 보는 SELECT 로 바꾼다."""
    lowered = sql.strip().lower()
    if lowered.startswith("delete from"):
        return "SELECT COUNT(*) AS n FROM " + sql.strip()[len("DELETE FROM") :]
    # UPDATE ... SET ... WHERE ...  →  SELECT COUNT(*) FROM <table> WHERE ...
    head, _, where = sql.partition("WHERE")
    tablename = head.split()[1]
    return f"SELECT COUNT(*) AS n FROM {tablename} WHERE {where}"


def run(args) -> int:
    today = args.date or utc_today()
    db = connect(args.db)

    rows = []
    for label, sql, params in plan(db, today):
        counted = db.one(count_sql(sql), params)
        n = int(counted["n"] or 0) if counted else 0
        if not args.dry_run and n:
            db.execute(sql, params)
        rows.append([label, str(n)])

    if args.dry_run:
        log("[step9] dry-run — 아무것도 지우지 않는다")
    else:
        db.commit()
        if args.vacuum:
            # VACUUM 은 트랜잭션 안에서 돌 수 없다.
            db.conn.isolation_level = None
            db.execute("VACUUM")
            db.conn.isolation_level = ""
        log(f"[step9] 정리 완료 · 총 {sum(int(r[1]) for r in rows)}행")

    if args.report:
        print(report(rows, today))

    db.close()
    return EXIT_OK


def report(rows, today: str) -> str:
    from store.dump import size_report

    return (
        f"DB 정리 ({today})\n"
        + table(rows, ["대상", "행 수"])
        + "\n\n"
        + size_report()
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step9] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
