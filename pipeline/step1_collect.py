"""step1 — 가중치 비례 수집.

6시간마다 실행. country_weights 를 읽어 예산을 배분하고, 배분량만큼 받아
원문은 R2 에, 메타데이터는 DB 에 넣는다.

    python -m pipeline.step1_collect --report
    python -m pipeline.step1_collect --dry-run --gdelt-fixture tests/fixtures/gdelt_artlist.json

--report 는 계획 대비 실제 수집량을 표로 낸다. 계획 1,000건인데 400건이
들어오면 즉시 알아야 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import config
from store.blobs import BlobStore
from store.db import connect, utcnow

from .budget import allocate, split_queries
from .common import EXIT_FAIL, EXIT_OK, base_parser, log, table, utc_today
from .sources import GdeltSource, RssSource


def build_parser():
    p = base_parser("가중치에 비례해 기사를 수집한다")
    p.add_argument("--gdelt-fixture", default=None, help="GDELT artlist 응답 대신 읽을 JSON")
    p.add_argument("--rss-fixture-dir", default=None, help="RSS 항목을 읽을 디렉토리")
    p.add_argument("--plan-only", action="store_true", help="배분만 하고 수집은 건너뛴다")
    return p


def plan_budget(db, date: str) -> tuple[dict[str, int], list]:
    """country_weights → fips별 할당량."""
    budget = config.sources()["budget"]
    top_n = int(budget["top_n_countries"])
    rows = db.weights_for(limit=top_n)
    if not rows:
        raise RuntimeError(
            "country_weights 가 비어 있다. `python -m pipeline.step0_weights` 를 먼저 돌려라."
        )
    alloc = allocate(
        [(r["fips"], float(r["weight"])) for r in rows],
        total=int(budget["articles_per_day"]),
        floor=int(budget["floor_per_country"]),
        cap=int(budget["cap_per_country"]),
        top_n=top_n,
    )
    return alloc.quotas, rows


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)

    quotas, _ = plan_budget(db, date)
    log(
        f"[step1] {date} · {len(quotas)}개국 · 계획 {sum(quotas.values())}건 "
        f"(floor {config.sources()['budget']['floor_per_country']}, "
        f"cap {config.sources()['budget']['cap_per_country']})"
    )
    if not args.dry_run:
        db.save_plan(date, quotas)

    if args.plan_only:
        if args.report:
            print(report(db, date, quotas))
        db.close()
        return EXIT_OK

    gdelt = GdeltSource(Path(args.gdelt_fixture) if args.gdelt_fixture else None)
    themes = list((config.sources().get("gdelt") or {}).get("themes_for_split") or [])
    known = db.known_urls()          # 이미 수집한 url 은 다시 받지 않는다
    blobs = BlobStore(local_only=args.dry_run)

    collected: dict[str, int] = {}
    query_counts: dict[str, int] = {}
    pending_blobs: list[dict] = []
    total_new = 0

    for fips, quota in sorted(quotas.items(), key=lambda kv: -kv[1]):
        if args.limit and total_new >= args.limit:
            break
        theme_plan = split_queries(quota, gdelt.maxrecords, themes)
        query_counts[fips] = len(theme_plan)
        per_query = -(-quota // len(theme_plan))
        rows: list[dict] = []
        seen_in_country: set[str] = set()

        for theme in theme_plan:
            for article in gdelt.fetch(fips, theme, per_query):
                canonical = article["url_canonical"]
                if canonical in known or canonical in seen_in_country:
                    continue
                seen_in_country.add(canonical)
                rows.append(article)
                if len(rows) >= quota:
                    break
            if len(rows) >= quota:
                break

        if args.limit:
            rows = rows[: max(0, args.limit - total_new)]

        collected[fips] = len(rows)
        total_new += len(rows)
        pending_blobs.extend(rows)
        known.update(r["url_canonical"] for r in rows)

    # RSS — 국가 코드 태그로 귀속시킨다
    rss = RssSource(Path(args.rss_fixture_dir) if args.rss_fixture_dir else None)
    rss_rows = []
    for row in rss.fetch_all():
        if row["url_canonical"] in known:
            continue
        known.add(row["url_canonical"])
        rss_rows.append(row)
    for r in rss_rows:
        collected[r["country_fips"]] = collected.get(r["country_fips"], 0) + 1
    pending_blobs.extend(rss_rows)
    total_new += len(rss_rows)

    if args.dry_run:
        log(f"[step1] dry-run — {total_new}건 수집 예정, DB/R2 에 쓰지 않는다")
    else:
        # 원문은 하루 1개 gzip JSONL 로. 기사 1건 = 객체 1개면 요청 수가 폭발한다.
        blob_key, start = blobs.append_day(date, pending_blobs)
        stored = 0
        for offset, row in enumerate(pending_blobs, start=start):
            if db.insert_article(
                {**row, "collected_at": utcnow(), "blob_key": blob_key, "blob_offset": offset}
            ):
                stored += 1
        for fips, n in collected.items():
            db.bump_collected(date, fips, n, query_counts.get(fips, 0))
        db.commit()
        log(f"[step1] 신규 {stored}건 저장 · blob {blob_key}")

    if args.report:
        print(report(db, date, quotas, collected))

    db.close()
    return EXIT_OK


def report(db, date: str, quotas: dict[str, int], collected: dict[str, int] | None = None) -> str:
    """계획 대비 실제. 여기가 어긋나면 LLM 호출 수 가정이 통째로 틀어진다."""
    if collected is None:
        collected = {r["fips"]: r["collected"] for r in db.plan_for(date)}
    rows = []
    for fips, quota in sorted(quotas.items(), key=lambda kv: -kv[1]):
        got = collected.get(fips, 0)
        country = config.by_fips(fips)
        rate = f"{got / quota:.0%}" if quota else "-"
        rows.append(
            [fips, country.name_ko if country else "?", str(quota), str(got), rate]
        )
    planned = sum(quotas.values())
    actual = sum(collected.get(f, 0) for f in quotas)
    head = (
        f"수집 계획 대비 실제 ({date}) — 계획 {planned}건 / 실제 {actual}건 "
        f"({actual / planned:.0%})" if planned else "수집 계획 없음"
    )
    warn = ""
    if planned and actual < planned * 0.6:
        warn = (
            "\n경고: 실제 수집량이 계획의 60% 미만이다. "
            "maxrecords·timespan·테마 분할을 확인해라."
        )
    return head + "\n" + table(rows, ["FIPS", "국가", "계획", "실제", "달성"]) + warn


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step1] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
