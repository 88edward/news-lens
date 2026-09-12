"""step0 — 국가 관심도 가중치 계산.

주 1회 실행. 이 결과가 step1 의 수집량 배분을 결정한다.

    python -m pipeline.step0_weights --report
    python -m pipeline.step0_weights --dry-run --fixture tests/fixtures/gdelt_timeline.json

값을 덮어쓰지 않고 날짜별로 쌓는다 — 가중치 변화 추이를 봐야 하기 때문이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import config
from store.db import connect
from weights import blend as blend_mod
from weights.base import get_provider

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, table, utc_today


def build_parser():
    p = base_parser("국가 관심도 가중치를 계산해 country_weights 에 저장한다")
    p.add_argument(
        "--fixture",
        default=None,
        help="GDELT 응답 대신 읽을 JSON 파일 (오프라인 테스트용)",
    )
    p.add_argument(
        "--top", type=int, default=25, help="--report 로 출력할 상위 국가 수"
    )
    return p


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)

    options: dict = {"db": db}
    if args.fixture:
        options["fixture"] = Path(args.fixture)

    specs = blend_mod.enabled_providers()
    providers = [get_provider(s["name"], options) for s in specs]
    log(
        f"[step0] {date} · provider {[p.name for p in providers]} "
        f"· lookback {blend_mod.formula().get('lookback_days', 7)}일"
    )

    results = blend_mod.blend(providers, options=options)
    if not results:
        log("[step0] 가중치가 하나도 계산되지 않았다. GDELT 응답과 name_en 매핑을 확인해라.")
        return EXIT_FAIL

    if args.limit:
        results = results[: args.limit]

    if args.dry_run:
        log(f"[step0] dry-run — {len(results)}개국 계산, DB에 쓰지 않는다")
    else:
        n = db.save_weights(date, [r.as_row() for r in results])
        log(f"[step0] country_weights 에 {n}행 저장 ({date})")

    if args.report:
        print(report(results, top=args.top, date=date))

    db.close()
    return EXIT_OK


def report(results, top: int = 25, date: str = "") -> str:
    rows = []
    for i, r in enumerate(results[:top], start=1):
        country = config.by_fips(r.fips)
        rows.append(
            [
                str(i),
                r.fips,
                r.iso2,
                country.name_ko if country else "?",
                f"{r.weight:.3f}",
                f"{r.attention:.3f}",
                f"{r.surge:.3f}",
                f"{r.surge_raw:.2f}x",
            ]
        )
    head = f"상위 {min(top, len(results))}개국 가중치"
    if date:
        head += f" ({date})"
    return (
        head
        + "\n"
        + table(
            rows,
            ["#", "FIPS", "ISO", "국가", "weight", "attention", "surge", "어제/평소"],
        )
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 — 크론에서 원인이 보여야 한다
        log(f"[step0] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
