"""step2 — 룰 필터. LLM 을 쓰지 않는다.

1,000건을 ~700건으로 줄인다. 여기서 LLM 을 부르면 비용 구조가 무너진다.

걸러낸 기사마다 이유를 filter_log 에 남긴다. 이게 없으면
"왜 이 기사가 안 들어왔지?" 를 영원히 답할 수 없고, 임계값을 손댈 근거도 없다.
"""
from __future__ import annotations

import sys
from collections import Counter

import config
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, table, utc_today
from .sources import canonical_url

#: 사유 코드. filter_log.reason 에 그대로 들어간다.
DUPLICATE = "duplicate"
LANGUAGE = "language"
DOMAIN = "domain"
TOO_SHORT_TITLE = "short_title"
TOO_SHORT_LEAD = "short_lead"
NO_COUNTRY = "no_country"


def build_parser():
    p = base_parser("룰 기반으로 기사를 걸러낸다 (LLM 없음)")
    p.add_argument(
        "--reprocess", action="store_true", help="이미 필터를 거친 기사도 다시 본다"
    )
    return p


class Rules:
    """config/pipeline.yaml 의 filter 섹션을 그대로 옮긴 것. 숫자를 코드에 박지 않는다."""

    def __init__(self):
        f = config.pipeline().get("filter") or {}
        self.languages = {str(x).lower() for x in (f.get("languages") or [])}
        self.min_title = int(f.get("min_title_chars", 0))
        self.min_lead = int(f.get("min_lead_chars", 0))
        self.blocklist = {str(d).lower() for d in (f.get("domain_blocklist") or [])}
        self.allowlist = {str(d).lower() for d in (f.get("domain_allowlist") or [])}

    def check(self, row: dict, seen: set[str]) -> tuple[str, str] | None:
        """통과하면 None, 걸리면 (사유, 상세)."""
        canonical = row.get("url_canonical") or canonical_url(row.get("url", ""))
        if canonical in seen:
            return DUPLICATE, canonical
        seen.add(canonical)

        domain = (row.get("domain") or "").lower()
        if self.allowlist and domain not in self.allowlist:
            return DOMAIN, f"화이트리스트 밖: {domain}"
        if domain in self.blocklist:
            return DOMAIN, f"블록리스트: {domain}"

        lang = (row.get("language") or "").lower()
        if self.languages:
            # GDELT 는 'English' 처럼 이름으로 준다. 코드와 이름을 둘 다 받는다.
            normalized = {lang, lang[:2], LANGUAGE_NAMES.get(lang, "")}
            if not (normalized & self.languages):
                return LANGUAGE, lang or "(없음)"

        title = (row.get("title") or "").strip()
        if len(title) < self.min_title:
            return TOO_SHORT_TITLE, f"{len(title)}자 < {self.min_title}"

        lead = (row.get("lead") or "").strip()
        # GDELT artlist 는 리드를 주지 않는다. 리드가 아예 없는 소스까지
        # 길이로 떨어뜨리면 수집분 전체가 날아간다 — 있을 때만 본다.
        if lead and len(lead) < self.min_lead:
            return TOO_SHORT_LEAD, f"{len(lead)}자 < {self.min_lead}"

        if not (row.get("country_fips") or "").strip():
            return NO_COUNTRY, "국가 귀속 실패"
        return None


LANGUAGE_NAMES = {
    "english": "en", "korean": "ko", "japanese": "ja", "chinese": "zh",
    "german": "de", "french": "fr", "spanish": "es", "portuguese": "pt",
    "russian": "ru", "arabic": "ar", "italian": "it", "dutch": "nl",
}


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)
    rules = Rules()

    statuses = ["collected"] + (["filtered", "embedded", "clustered"] if args.reprocess else [])
    rows = []
    for status in statuses:
        rows.extend(db.articles_by_status(status, args.limit))
    if args.limit:
        rows = rows[: args.limit]

    log(f"[step2] {date} · 대상 {len(rows)}건")
    seen: set[str] = set()
    if not args.reprocess:
        # 이전 실행에서 이미 통과한 것들과도 중복을 봐야 한다
        seen = {
            r["url_canonical"]
            for r in db.query(
                "SELECT url_canonical FROM articles WHERE status IN ('filtered','embedded','clustered')"
            )
        }

    reasons: Counter[str] = Counter()
    passed = 0
    for row in rows:
        verdict = rules.check(dict(row), seen)
        if verdict is None:
            passed += 1
            if not args.dry_run:
                db.set_article_status(row["id"], "filtered")
            continue
        reason, detail = verdict
        reasons[reason] += 1
        if not args.dry_run:
            db.log_filtered(date, row["id"], row["url"], reason, detail)
            db.set_article_status(row["id"], "rejected")

    if not args.dry_run:
        db.commit()

    rate = passed / len(rows) if rows else 0.0
    log(f"[step2] 통과 {passed}건 / {len(rows)}건 ({rate:.0%}) · 제거 {sum(reasons.values())}건")
    if rows and rate < 0.3:
        log("[step2] 경고: 통과율이 30% 미만이다. pipeline.yaml 의 filter 임계값을 확인해라.")

    if args.report:
        print(report(reasons, passed, len(rows), date))

    db.close()
    return EXIT_OK


def report(reasons: Counter, passed: int, total: int, date: str) -> str:
    rows = [[r, str(n), f"{n / total:.1%}" if total else "-"] for r, n in reasons.most_common()]
    rows.append(["(통과)", str(passed), f"{passed / total:.1%}" if total else "-"])
    return f"필터 결과 ({date}) — 입력 {total}건\n" + table(rows, ["사유", "건수", "비율"])


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step2] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
