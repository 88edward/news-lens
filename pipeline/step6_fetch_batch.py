"""step6 — 배치 수거.

아직 PENDING 이면 **exit code 75** 로 끝낸다. 워크플로가 그걸 보고 재시도한다.
실패(1)와 대기(75)를 구분하지 않으면 크론이 조용히 이슈를 만들거나,
반대로 진짜 실패를 대기로 착각한다.

스키마 검증에 실패한 응답은 버리지 않는다. raw 그대로 남기고 실패 카운트를 올린다.
프롬프트를 고칠 근거가 거기에만 있다.

    python -m pipeline.step6_fetch_batch --report
"""
from __future__ import annotations

import sys

import config
from llm import cost
from llm.base import BatchStatus
from llm.registry import get_client
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, table, utc_today
from .schema import parse_and_validate

ROLE = "event_analysis"


def exit_pending() -> int:
    return int((config.pipeline().get("batch") or {}).get("poll_exit_code_pending", 75))


def build_parser():
    return base_parser("제출한 batch 를 폴링해 결과를 events 에 저장한다")


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)

    open_batches = [r for r in db.open_batches() if r["role"] == ROLE]
    if not open_batches:
        log("[step6] 수거할 배치가 없다")
        db.close()
        return EXIT_OK

    client = get_client(ROLE)
    schema = config.schema_for(ROLE)
    ledger = cost.ledger(db=db)

    pending: list[str] = []
    stored = 0
    failures = 0
    reasons: list[tuple[str, str]] = []

    for run_row in open_batches:
        batch_id = run_row["batch_id"]

        if args.dry_run:
            log(f"[step6] dry-run — {batch_id} 를 조회하지 않는다")
            continue

        results = client.fetch_batch(batch_id)

        if results.status is BatchStatus.PENDING:
            log(f"[step6] {batch_id} 아직 처리 중")
            db.set_batch_status(batch_id, "pending")
            pending.append(batch_id)
            continue

        if results.status is BatchStatus.FAILED:
            log(f"[step6] {batch_id} 배치 자체가 실패했다")
            db.set_batch_status(batch_id, "failed")
            recovered = db.release_submitted(batch_id)
            log(f"[step6] 사건 {recovered}건을 pending 으로 되돌린다 — 다음 step5 가 다시 제출한다")
            continue

        # 완료인데 결과가 하나도 없다. 여기서 'ended' 로 닫아 버리면 그 배치의
        # 사건들은 submitted 상태로 영원히 남는다 — 재제출도, 분석도 되지 않고
        # 사이트에서 조용히 사라진다. 실패로 보고 되돌린다.
        if not results.items:
            stranded = db.release_submitted(batch_id)
            db.set_batch_status(batch_id, "failed")
            log(
                f"[step6] {batch_id} 가 결과 0건으로 끝났다. "
                f"사건 {stranded}건을 pending 으로 되돌린다 — 다음 step5 가 다시 제출한다."
            )
            continue

        for item in results.items:
            if not item.ok:
                failures += 1
                reasons.append((item.custom_id, item.error or "unknown"))
                db.save_event_analysis(
                    item.custom_id, analysis=None, raw=item.error or "", schema_ok=False
                )
                continue

            payload, errors = parse_and_validate(item.text, schema)
            if errors:
                failures += 1
                reasons.append((item.custom_id, errors[0]))
                # 버리지 않는다. 원문을 남겨야 프롬프트를 고칠 수 있다.
                db.save_event_analysis(
                    item.custom_id, analysis=None, raw=item.text, schema_ok=False
                )
                continue

            db.save_event_analysis(
                item.custom_id, analysis=payload, raw=item.text, schema_ok=True
            )
            stored += 1

        ledger.record(ROLE, results.usage, note=f"batch {batch_id}")
        db.set_batch_status(batch_id, "ended")
        db.commit()
        log(
            f"[step6] {batch_id} 수거 · 성공 {stored}건 · 검증실패 {failures}건 "
            f"· 비용 ${cost.price_of(ROLE, results.usage):.4f}"
        )

    if args.report:
        print(report(db, date, stored, failures, reasons))

    db.close()

    if pending:
        log(f"[step6] {len(pending)}개 배치가 아직 대기 중 — exit {exit_pending()} 로 재시도를 요청한다")
        return exit_pending()
    return EXIT_OK


def report(db, date: str, stored: int, failures: int, reasons) -> str:
    total = stored + failures
    rate = f"{failures / total:.0%}" if total else "-"
    out = [
        f"배치 수거 ({date}) — 성공 {stored}건 / 검증실패 {failures}건 (실패율 {rate})"
    ]
    if failures:
        out.append("")
        out.append("검증 실패 사유 (프롬프트를 고칠 근거다):")
        out.append(table([[cid, msg[:90]] for cid, msg in reasons[:20]], ["사건", "사유"]))
        out.append("")
        out.append(
            "원문은 events.raw_response 에 남아 있다: "
            "SELECT id, raw_response FROM events WHERE schema_ok = 0"
        )
    if total and failures / total > 0.2:
        out.append("")
        out.append("경고: 검증 실패율이 20%를 넘는다. 프롬프트나 스키마를 손봐야 한다.")
    return "\n".join(out)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step6] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
