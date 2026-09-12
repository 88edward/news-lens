"""step8 — 글로브 JSON + 정적 사이트 빌드.

    python -m pipeline.step8_build_site --from-fixtures   ← 샘플만으로 dist 생성
    python -m pipeline.step8_build_site --date 2026-09-12

원문은 절대 싣지 않는다. 요약 + 출처 링크만.
최근 N일(pipeline.yaml 의 retention.static_build_days)만 정적으로 굽는다.
그보다 오래된 것은 Worker 가 처리한다.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, utc_today

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
TEMPLATE_DIR = SITE_DIR / "templates"
STATIC_DIR = SITE_DIR / "static"
DIST_DIR = SITE_DIR / "dist"


def build_parser():
    p = base_parser("globe.json 과 정적 페이지를 만든다")
    p.add_argument(
        "--from-fixtures",
        action="store_true",
        help="DB 없이 샘플 데이터로 dist 를 만든다 (프론트 개발용)",
    )
    p.add_argument("--out", default=None, help="출력 디렉토리 (기본 site/dist)")
    p.add_argument("--days", type=int, default=None, help="정적으로 구울 일수")
    return p


# ── 데이터 조립 ─────────────────────────────────────────────────────────


def globe_payload(db, date: str) -> dict:
    """글로브가 읽는 단일 파일.

    마커 크기는 프론트에서 sqrt(article_count) 로 정규화한다 — 선형이면
    미국만 거대해져 나머지가 안 보인다. 여기서는 원시값을 넘긴다.
    """
    weights = {r["fips"]: r for r in db.weights_for()}
    countries = []

    for row in db.query(
        """SELECT primary_country AS fips,
                  COUNT(*) AS event_count,
                  SUM(article_count) AS article_count,
                  AVG(tone) AS avg_tone
             FROM events
            WHERE date = ? AND status = 'analyzed'
            GROUP BY primary_country""",
        (date,),
    ):
        country = config.by_fips(row["fips"])
        if country is None:
            continue
        weight_row = weights.get(row["fips"])
        top = db.one(
            """SELECT id, headline, source_count FROM events
                WHERE date = ? AND primary_country = ? AND status = 'analyzed'
                ORDER BY source_count DESC, article_count DESC LIMIT 1""",
            (date, row["fips"]),
        )
        countries.append(
            {
                "iso2": country.iso2,
                "name_ko": country.name_ko,
                "lat": country.lat,
                "lng": country.lng,
                "weight": round(float(weight_row["weight"]), 4) if weight_row else 0.0,
                "article_count": int(row["article_count"] or 0),
                "event_count": int(row["event_count"] or 0),
                "avg_tone": round(float(row["avg_tone"]), 2)
                if row["avg_tone"] is not None
                else 0.0,
                "surge": round(float(weight_row["surge_raw"]), 2) if weight_row else 0.0,
                "top_event": {
                    "id": top["id"],
                    "headline": top["headline"],
                    "source_count": top["source_count"],
                }
                if top
                else None,
            }
        )

    countries.sort(key=lambda c: -c["weight"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": date,
        "countries": countries,
    }


def country_payload(db, date: str, fips: str) -> dict:
    """마커 클릭 시 패널이 읽는 파일. 요약 + 출처 링크만."""
    country = config.by_fips(fips)
    events = []
    for event in db.query(
        """SELECT * FROM events
            WHERE date = ? AND primary_country = ? AND status = 'analyzed'
            ORDER BY source_count DESC, article_count DESC""",
        (date, fips),
    ):
        analysis = json.loads(event["analysis"] or "{}")
        sources = [
            {"domain": a["domain"], "title": a["title"], "url": a["url"]}
            for a in db.articles_of_event(event["id"])
        ]
        events.append(
            {
                "id": event["id"],
                "headline": event["headline"],
                # 요약은 배경 문단을 쓴다. 기사 원문이 아니다.
                "summary": analysis.get("배경", ""),
                "category": event["category"],
                "tone": event["tone"],
                "source_count": event["source_count"],
                "sources": sources[:12],
            }
        )
    return {
        "iso2": country.iso2 if country else fips,
        "name_ko": country.name_ko if country else fips,
        "date": date,
        "events": events,
    }


def event_payload(db, event) -> dict:
    analysis = json.loads(event["analysis"] or "{}")
    country = config.by_fips(event["primary_country"])
    return {
        "id": event["id"],
        "date": event["date"],
        "country": {
            "iso2": country.iso2 if country else "",
            "name_ko": country.name_ko if country else event["primary_country"],
        },
        "headline": event["headline"],
        "category": event["category"],
        "tone": event["tone"],
        "confidence": event["confidence"],
        "source_count": event["source_count"],
        "analysis": analysis,
        "sources": [
            {"domain": a["domain"], "title": a["title"], "url": a["url"]}
            for a in db.articles_of_event(event["id"])
        ],
    }


# ── 렌더링 ──────────────────────────────────────────────────────────────


def jinja_env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tone_label"] = tone_label
    env.filters["tone_class"] = tone_class
    return env


def tone_label(tone) -> str:
    if tone is None:
        return "중립"
    if tone <= -5:
        return "매우 부정"
    if tone <= -1.5:
        return "부정"
    if tone < 1.5:
        return "중립"
    if tone < 5:
        return "긍정"
    return "매우 긍정"


def tone_class(tone) -> str:
    if tone is None:
        return "tone-neutral"
    if tone <= -5:
        return "tone-verynegative"
    if tone <= -1.5:
        return "tone-negative"
    if tone < 1.5:
        return "tone-neutral"
    return "tone-positive"


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def write_page(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def build(db, out: Path, dates: list[str]) -> dict:
    env = jinja_env()
    site_cfg = config.pipeline().get("site") or {}
    latest = dates[0]

    globe = globe_payload(db, latest)
    write_json(out / "data" / "globe.json", globe)

    counts = {"countries": 0, "events": 0, "briefings": 0}

    # 국가별 데이터 + 폴백 페이지
    for country in globe["countries"]:
        fips = config.iso_to_fips(country["iso2"])
        payload = country_payload(db, latest, fips)
        write_json(out / "data" / "country" / f"{country['iso2']}.json", payload)
        write_page(
            out / "country" / country["iso2"] / "index.html",
            env.get_template("country.html").render(
                country=payload, globe=globe, site=site_cfg
            ),
        )
        counts["countries"] += 1

    # 사건 상세 — 최근 N일
    for date in dates:
        for event in db.events_for(date, status="analyzed"):
            payload = event_payload(db, event)
            write_page(
                out / "event" / event["id"] / "index.html",
                env.get_template("event.html").render(event=payload, site=site_cfg),
            )
            counts["events"] += 1

        briefing = db.briefing_for(date)
        if briefing and briefing["schema_ok"]:
            write_page(
                out / "briefing" / date / "index.html",
                env.get_template("briefing.html").render(
                    briefing=json.loads(briefing["content"]),
                    date=date,
                    site=site_cfg,
                ),
            )
            counts["briefings"] += 1

    # 첫 진입 화면
    write_page(
        out / "index.html",
        env.get_template("index.html").render(
            globe=globe, site=site_cfg, dates=dates, briefing_date=latest
        ),
    )

    # 정적 자산
    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, out / "static", dirs_exist_ok=True)

    return counts


# ── fixtures 모드 ───────────────────────────────────────────────────────


def seed_fixture_db(db, date: str) -> None:
    """DB 없이 프론트를 보려고 만드는 샘플 데이터.

    글로브가 실제로 어떻게 보이는지 확인하려면 국가 수와 값의 분포가
    그럴듯해야 한다 — 미국 하나만 큰 상태로는 마커 크기 정규화를 볼 수 없다.
    """
    rng = random.Random(12)
    rows = config.countries()[:22]
    weights = []
    for i, country in enumerate(rows):
        weight = max(0.05, 1.0 - i * 0.042)
        surge = 2.6 if i in (4, 9, 14) else rng.uniform(0.8, 1.4)
        weights.append(
            {
                "fips": country.fips,
                "iso2": country.iso2,
                "attention": round(weight, 3),
                "surge": round(min(1.0, max(0.0, (surge - 1) / 2)), 3),
                "surge_raw": round(surge, 2),
                "weight": round(weight, 3),
                "provider_breakdown": {"fixture": {"blend": 1.0}},
            }
        )
    db.save_weights(date, weights)

    HEADLINES = [
        ("중앙은행이 기준금리를 동결하며 물가 위험을 언급했다", "economy", -2.4),
        ("무역 협상이 교착 상태에 빠지며 관세 시한이 다가온다", "politics", -4.1),
        ("주요 지수가 기술주 강세에 힘입어 반등했다", "markets", 3.2),
        ("에너지 수급 불안으로 전력 요금 인상이 예고됐다", "economy", -3.0),
        ("국경 지역 교전이 사흘째 이어지며 민간인 피해가 보고됐다", "conflict", -7.5),
        ("반도체 수출이 예상을 웃돌며 무역수지가 개선됐다", "economy", 4.0),
        ("통화가 사상 최저치로 떨어지며 당국이 개입을 시사했다", "markets", -5.2),
        ("대규모 인프라 투자 계획이 의회를 통과했다", "politics", 2.6),
    ]
    n = 0
    for i, country in enumerate(rows):
        for k in range(rng.randint(1, 5)):
            n += 1
            headline, category, tone = HEADLINES[(i + k) % len(HEADLINES)]
            event_id = f"evt_{date.replace('-', '')}_{n:04d}"
            source_count = rng.randint(2, 9)
            db.insert_event(
                {
                    "id": event_id,
                    "date": date,
                    "primary_country": country.fips,
                    "article_count": source_count + rng.randint(0, 6),
                    "source_count": source_count,
                    "representatives": "[]",
                }
            )
            db.save_event_analysis(
                event_id,
                analysis={
                    "한줄요약": f"{country.name_ko}: {headline}",
                    "핵심사실": ["여러 매체가 공통으로 보도한 사실", "확인된 수치"],
                    "배경": (
                        f"{country.name_ko}에서 이어져 온 흐름이 이번 주 들어 뚜렷해졌다. "
                        "정책 당국은 아직 공식 입장을 내놓지 않았다."
                    ),
                    "이해관계자": [{"이름": "정책 당국", "입장": "상황을 주시 중"}],
                    "영향분석": "단기적으로 금융시장 변동성이 커질 수 있다.",
                    "출처간_불일치": [
                        {"쟁점": "규모", "설명": "매체별로 제시한 수치가 다르다"}
                    ],
                    "카테고리": category,
                    "신뢰도": round(rng.uniform(0.55, 0.95), 2),
                    "논조점수": round(tone + rng.uniform(-1, 1), 1),
                },
                raw="{}",
                schema_ok=True,
            )
            for s in range(source_count):
                domain = f"example-news-{s + 1}.com"
                article_id = f"{event_id}_a{s}"
                db.insert_article(
                    {
                        "id": article_id,
                        "url": f"https://{domain}/story/{event_id}-{s}",
                        "url_canonical": f"https://{domain}/story/{event_id}-{s}",
                        "domain": domain,
                        "title": f"{headline} ({s + 1})",
                        "lead": "",
                        "language": "en",
                        "collected_at": f"{date}T06:00:00+00:00",
                        "country_fips": country.fips,
                        "source_kind": "gdelt",
                    }
                )
                db.assign_event(article_id, event_id)

    db.save_briefing(
        date,
        "주요국 통화정책이 동시에 관망으로 돌아선 하루",
        {
            "헤드라인": "주요국 통화정책이 동시에 관망으로 돌아선 하루",
            "오늘의_3대_흐름": [
                {
                    "제목": "긴축 유지",
                    "설명": "여러 중앙은행이 같은 방향으로 관망을 택했다는 점이 공통이다.",
                    "관련사건": [f"evt_{date.replace('-', '')}_0001"],
                },
                {
                    "제목": "신흥국 통화 약세",
                    "설명": "자본 유출 우려가 커지며 몇몇 통화가 동시에 약세를 보였다.",
                    "관련사건": [],
                },
            ],
            "지역별_요약": [
                {"지역": "동아시아", "요약": "정책 변화 없이 관망세가 이어졌고 환율만 움직였다."},
                {"지역": "유럽", "요약": "에너지 가격과 재정 논의가 동시에 시장을 흔들었다."},
            ],
            "주목할_신호": [
                {"신호": "채권 스프레드 확대", "근거": "아직 소수 매체만 다루고 있다"}
            ],
        },
        raw="{}",
        schema_ok=True,
    )
    db.commit()


# ── 실행 ────────────────────────────────────────────────────────────────


def run(args) -> int:
    date = args.date or utc_today()
    out = Path(args.out) if args.out else DIST_DIR
    days = args.days or int(
        (config.pipeline().get("retention") or {}).get("static_build_days", 30)
    )

    if args.from_fixtures:
        db = connect(memory=True)
        seed_fixture_db(db, date)
        log(f"[step8] --from-fixtures · 샘플 데이터로 빌드한다 ({date})")
    else:
        db = connect(args.db)

    start = date_cls.fromisoformat(date)
    dates = [(start - timedelta(days=i)).isoformat() for i in range(days)]
    dates = [d for d in dates if db.events_for(d, status="analyzed") or d == date]

    if not db.events_for(date, status="analyzed"):
        log(f"[step8] {date} 에 분석된 사건이 없다. step6 을 먼저 돌려라.")
        if not args.from_fixtures:
            db.close()
            return EXIT_FAIL

    if args.dry_run:
        globe = globe_payload(db, date)
        log(
            f"[step8] dry-run — 국가 {len(globe['countries'])}개, "
            f"{len(dates)}일치 페이지 생성 예정. 파일을 쓰지 않는다."
        )
        db.close()
        return EXIT_OK

    if out.exists():
        shutil.rmtree(out)
    counts = build(db, out, dates)

    log(
        f"[step8] {out} 생성 · 국가 {counts['countries']}개 "
        f"· 사건 {counts['events']}개 · 브리핑 {counts['briefings']}개"
    )
    if args.report:
        print(report(out, counts))

    db.close()
    return EXIT_OK


def report(out: Path, counts: dict) -> str:
    files = sorted(p for p in out.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    lines = [
        f"빌드 결과: {out}",
        f"  파일 {len(files)}개 · {total / 1024:.0f}KB",
        f"  국가 {counts['countries']} · 사건 {counts['events']} · 브리핑 {counts['briefings']}",
        "",
        "로컬에서 열기:",
        f"  python -m http.server -d {out} 8000   →  http://localhost:8000",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step8] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
