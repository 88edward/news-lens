"""step7 — 일일 종합. 하루에 **한 번만** 부른다.

models.yaml 의 mode 를 따른다.
  - mode: sync  → 즉시 호출하고 끝. 하루 1회이므로 batch 대비 차액은 월 $2 미만이다.
  - mode: batch → 제출 후 폴링. 아직이면 step6 과 같은 exit 75 로 재시도를 요청한다.

    python -m pipeline.step7_synthesize --report
"""
from __future__ import annotations

import json
import sys

import config
from llm import cost
from llm.base import BatchRequest, BatchStatus
from llm.registry import get_client
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, utc_today
from .schema import parse_and_validate
from .step6_fetch_batch import exit_pending

ROLE = "daily_synthesis"


def build_parser():
    p = base_parser("그날 사건을 모아 일일 브리핑을 만든다")
    p.add_argument("--force", action="store_true", help="이미 브리핑이 있어도 다시 만든다")
    return p


def render_events(events) -> str:
    lines = [f"분석된 사건 {len(events)}건:", ""]
    for event in events:
        country = config.by_fips(event["primary_country"])
        tone = event["tone"]
        lines.append(
            f"- {event['id']} | {country.name_ko if country else event['primary_country']}"
            f" | {event['category'] or '-'}"
            f" | 논조 {tone if tone is not None else '-'}"
            f" | 매체 {event['source_count']}곳"
        )
        lines.append(f"  {event['headline']}")
    return "\n".join(lines)


def validate_event_ids(payload: dict, known: set[str]) -> list[str]:
    """브리핑이 없는 사건 id 를 지어냈는지 본다.

    스키마는 형식만 본다. 실제로 존재하는 id 인지는 여기서만 알 수 있고,
    없는 id 가 사이트에 실리면 죽은 링크가 된다.
    """
    errors = []
    for trend in payload.get("오늘의_3대_흐름") or []:
        for eid in trend.get("관련사건") or []:
            if eid not in known:
                errors.append(f"오늘의_3대_흐름: 없는 사건 id '{eid}'")
    return errors


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)

    if db.briefing_for(date) and not args.force:
        log(f"[step7] {date} 브리핑이 이미 있다 (--force 로 덮어쓴다)")
        db.close()
        return EXIT_OK

    events = db.events_for(date, status="analyzed")
    if args.limit:
        events = events[: args.limit]
    if not events:
        log(f"[step7] {date} · 분석된 사건이 없다. step6 을 먼저 돌려라.")
        db.close()
        return EXIT_OK

    spec = config.role(ROLE)
    system = config.prompt_text(ROLE)
    user = render_events(events)
    schema = config.schema_for(ROLE)
    known_ids = {e["id"] for e in events}

    ledger = cost.ledger(db=db, day=date)
    ledger.preflight(
        ROLE,
        in_tokens=(len(system) + len(user)) // 4,
        out_tokens=int(spec.get("max_output_tokens") or 8000) // 2,
        n=1,
    )

    if args.dry_run:
        log(f"[step7] dry-run — 사건 {len(events)}건으로 브리핑 1회 호출 예정")
        db.close()
        return EXIT_OK

    client = get_client(ROLE)

    if spec["mode"] == "sync":
        completion = client.complete(system, user)
        text, usage = completion.text, completion.usage
    else:
        text, usage, rc = _via_batch(db, client, date, system, user, spec)
        if rc is not None:
            db.close()
            return rc

    ledger.record(ROLE, usage, note=f"{len(events)}개 사건")

    payload, errors = parse_and_validate(text, schema)
    if payload is not None and not errors:
        errors = validate_event_ids(payload, known_ids)

    if errors:
        log(f"[step7] 스키마 검증 실패 {len(errors)}건: {errors[:3]}")
        db.save_briefing(date, "", {}, raw=text, schema_ok=False)
        if args.report:
            print("검증 실패 — 원문은 briefings.raw_response 에 남아 있다")
            for e in errors[:10]:
                print(f"  - {e}")
        db.close()
        return EXIT_FAIL

    db.save_briefing(date, payload["헤드라인"], payload, raw=text, schema_ok=True)
    log(f"[step7] 브리핑 저장: {payload['헤드라인']}")

    if args.report:
        print(report(payload, date, len(events)))

    db.close()
    return EXIT_OK


def _via_batch(db, client, date: str, system: str, user: str, spec):
    """batch 모드: 제출 기록이 없으면 제출하고, 있으면 수거를 시도한다."""
    existing = [
        r for r in db.open_batches() if r["role"] == ROLE and r["date"] == date
    ]
    if not existing:
        batch_id = client.submit_batch(
            [
                BatchRequest(
                    custom_id=f"briefing_{date}",
                    system=system,
                    user=user,
                    max_output_tokens=int(spec.get("max_output_tokens") or 8000),
                )
            ]
        )
        db.record_run(
            day=date, step="step7", role=ROLE, model=spec["model"],
            batch_id=batch_id, status="submitted", note="일일 종합 1건",
        )
        log(f"[step7] batch 제출 {batch_id} — 수거는 다음 실행에서")
        return "", None, exit_pending()

    batch_id = existing[0]["batch_id"]
    results = client.fetch_batch(batch_id)
    if results.status is BatchStatus.PENDING:
        log(f"[step7] {batch_id} 아직 처리 중")
        db.set_batch_status(batch_id, "pending")
        return "", None, exit_pending()
    if results.status is BatchStatus.FAILED or not results.items:
        db.set_batch_status(batch_id, "failed")
        log(f"[step7] {batch_id} 배치 실패")
        return "", None, EXIT_FAIL

    db.set_batch_status(batch_id, "ended")
    item = results.items[0]
    return item.text, results.usage, None


def report(payload: dict, date: str, n_events: int) -> str:
    out = [
        f"일일 브리핑 ({date}) — 사건 {n_events}건 기반",
        "",
        f"■ {payload['헤드라인']}",
        "",
        "오늘의 3대 흐름:",
    ]
    for trend in payload.get("오늘의_3대_흐름") or []:
        out.append(f"  · {trend['제목']}")
        out.append(f"    {trend['설명'][:160]}")
        out.append(f"    관련: {', '.join(trend.get('관련사건') or []) or '-'}")
    out.append("")
    out.append("지역별:")
    for region in payload.get("지역별_요약") or []:
        out.append(f"  · [{region['지역']}] {region['요약'][:120]}")
    signals = payload.get("주목할_신호") or []
    if signals:
        out.append("")
        out.append("주목할 신호:")
        for s in signals:
            out.append(f"  · {s['신호']} — {s['근거'][:100]}")
    return "\n".join(out)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step7] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
