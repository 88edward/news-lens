"""step5 — 사건 분석 배치 제출.

제출하고 **즉시 끝낸다.** 결과를 기다리지 않는다.
Batch API 는 반값이지만 비동기라서(통상 1~4h, 보장 24h) 러너를 켜둔 채
기다리면 그 비용이 절감액을 넘는다. 수거는 step6 의 몫이다.

    python -m pipeline.step5_submit_batch --dry-run --report
"""
from __future__ import annotations

import json
import sys

import config
from llm import cost
from llm.base import BatchRequest
from llm.registry import get_client
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, utc_today

ROLE = "event_analysis"


def build_parser():
    p = base_parser("분석되지 않은 사건으로 batch 를 만들어 제출한다")
    return p


def render_event(db, event) -> str:
    """사건 1개 + 대표 기사 3건을 프롬프트 입력으로 만든다.

    원문은 넣지 않는다. GDELT 가 본문을 주지 않기도 하고, 넣을 필요도 없다.
    """
    reps = json.loads(event["representatives"] or "[]")
    country = config.by_fips(event["primary_country"])
    lines = [
        f"사건 id: {event['id']}",
        f"국가: {country.name_ko if country else event['primary_country']}"
        f" ({event['primary_country']})",
        f"관련 기사 {event['article_count']}건 · 매체 {event['source_count']}곳",
        "",
        "대표 기사:",
    ]
    for i, rid in enumerate(reps, start=1):
        row = db.one("SELECT * FROM articles WHERE id = ?", (rid,))
        if row is None:
            continue
        lines.append(f"[{i}] 매체: {row['domain']}")
        lines.append(f"    제목: {row['title']}")
        if (row["lead"] or "").strip():
            lines.append(f"    리드: {row['lead'][:600]}")
        if row["published_at"]:
            lines.append(f"    발행: {row['published_at']}")
        lines.append("")
    return "\n".join(lines)


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)

    events = db.events_for(date, status="pending")
    if args.limit:
        events = events[: args.limit]

    if not events:
        log(f"[step5] {date} · 제출할 사건이 없다")
        db.close()
        return EXIT_OK

    system = config.prompt_text(ROLE)
    spec = config.role(ROLE)
    max_per_batch = int((config.pipeline().get("batch") or {}).get("max_requests_per_batch", 1000))

    requests = []
    for event in events[:max_per_batch]:
        requests.append(
            BatchRequest(
                custom_id=event["id"],
                system=system,
                user=render_event(db, event),
                max_output_tokens=int(spec.get("max_output_tokens") or 2000),
            )
        )

    # 비용은 부르기 전에 찍고, 상한을 넘으면 여기서 멈춘다.
    avg_in = sum(len(r.system) + len(r.user) for r in requests) // (4 * len(requests))
    ledger = cost.ledger(db=db, day=date)
    ledger.preflight(
        ROLE,
        in_tokens=avg_in,
        out_tokens=int(spec.get("max_output_tokens") or 2000) // 2,
        n=len(requests),
    )

    if args.dry_run:
        log(f"[step5] dry-run — 사건 {len(requests)}건 제출 예정, API 를 부르지 않는다")
        if args.report:
            print(report(requests))
        db.close()
        return EXIT_OK

    client = get_client(ROLE)
    batch_id = client.submit_batch(requests)

    db.mark_events_submitted([r.custom_id for r in requests], batch_id)
    db.record_run(
        day=date,
        step="step5",
        role=ROLE,
        model=spec["model"],
        batch_id=batch_id,
        status="submitted",
        note=f"{len(requests)}건 제출",
    )
    log(f"[step5] 제출 완료 batch_id={batch_id} · 사건 {len(requests)}건 · 기다리지 않고 종료")

    if args.report:
        print(report(requests))

    db.close()
    return EXIT_OK


def report(requests) -> str:
    out = [f"제출 배치 — {len(requests)}건", ""]
    for req in requests[:3]:
        out.append(f"── {req.custom_id} " + "─" * 40)
        out.append(req.user.strip()[:600])
        out.append("")
    if len(requests) > 3:
        out.append(f"... 외 {len(requests) - 3}건")
    return "\n".join(out)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except cost.CostLimitExceeded as exc:
        log(f"[step5] 비용 상한으로 중단: {exc}")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001
        log(f"[step5] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
