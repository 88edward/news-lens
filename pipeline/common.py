"""step 들이 공유하는 최소한의 것.

여기에 파이프라인 로직을 넣지 마라 — 인자 파싱과 출력 포맷만.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

EXIT_OK = 0
EXIT_FAIL = 1


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def base_parser(description: str) -> argparse.ArgumentParser:
    """모든 step 이 공통으로 받는 인자.

    --dry-run 은 장식이 아니다. API 키가 하나도 없어도 끝까지 돌아야 한다.
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="외부 호출과 쓰기를 하지 않는다. API 키 없이도 끝까지 돈다.",
    )
    p.add_argument(
        "--limit", type=int, default=None, help="처리 건수 상한 (디버깅용)"
    )
    p.add_argument("--date", default=None, help="기준 날짜 YYYY-MM-DD (기본: 오늘 UTC)")
    p.add_argument("--db", default=None, help="SQLite 경로 (기본: 환경변수 또는 레포 루트)")
    p.add_argument("--report", action="store_true", help="결과를 표로 출력한다")
    return p


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def table(rows: list[list[str]], headers: list[str]) -> str:
    """의존성 없는 고정폭 표. --report 출력용."""
    if not rows:
        return "(없음)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(str(cell)))
    out = [
        "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        out.append("  ".join(_pad(str(c), widths[i]) for i, c in enumerate(row)))
    return "\n".join(out)


def _width(text: str) -> int:
    """한글은 터미널에서 두 칸을 먹는다."""
    return sum(2 if ord(ch) > 0x1100 and _is_wide(ch) else 1 for ch in text)


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))
