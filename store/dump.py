"""DB를 레포에 싣기 위한 텍스트 덤프.

왜 SQLite 파일을 그대로 커밋하지 않는가:
git 은 바이너리를 델타 압축하지 못한다. SQLite 파일은 한 행만 바뀌어도
페이지 여기저기가 달라지므로, 하루 5번 커밋하면 매번 DB 전체 크기가
히스토리에 쌓인다. 1년이면 기가바이트 단위가 된다.

SQL 텍스트 덤프는 대부분 append 라서 git 이 줄 단위로 델타를 잡는다.
같은 데이터를 담고도 히스토리 증가량이 한 자릿수 배 작다.

    python -m store.dump export        # DB → data/news-lens.sql
    python -m store.dump import        # data/news-lens.sql → DB
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DUMP = REPO_ROOT / "data" / "news-lens.sql"


def dump_path() -> Path:
    return Path(os.environ.get("NEWS_LENS_DUMP", DEFAULT_DUMP))


def db_path() -> Path:
    from .db import DEFAULT_DB_PATH

    return Path(os.environ.get("NEWS_LENS_DB", DEFAULT_DB_PATH))


def export(db: Path | str | None = None, out: Path | str | None = None) -> Path:
    """DB → SQL 텍스트."""
    source = Path(db) if db else db_path()
    target = Path(out) if out else dump_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        # 아직 DB 가 없으면 빈 덤프를 남긴다 — 다음 실행이 복원할 게 있어야 한다.
        target.write_text("-- (비어 있음)\n", encoding="utf-8")
        return target

    conn = sqlite3.connect(source)
    try:
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            for line in conn.iterdump():
                fh.write(line + "\n")
    finally:
        conn.close()
    return target


def restore(sql: Path | str | None = None, db: Path | str | None = None) -> Path:
    """SQL 텍스트 → DB. 기존 DB 파일은 지우고 새로 만든다."""
    source = Path(sql) if sql else dump_path()
    target = Path(db) if db else db_path()

    if not source.exists():
        # 첫 실행이다. 빈 DB 를 만들어 두면 step 들이 스키마를 잡는다.
        from .db import connect

        connect(target).close()
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(target) + suffix)
        stale.unlink(missing_ok=True)

    conn = sqlite3.connect(target)
    try:
        conn.executescript(source.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    # 덤프가 오래돼 스키마가 빠져 있을 수 있다. connect() 가 보충한다.
    from .db import connect

    connect(target).close()
    return target


def size_report(path: Path | None = None) -> str:
    path = path or dump_path()
    if not path.exists():
        return f"{path}: 없음"
    kb = path.stat().st_size / 1024
    lines = sum(1 for _ in path.open(encoding="utf-8"))
    warn = ""
    if kb > 50_000:
        warn = (
            "\n경고: 덤프가 50MB를 넘었다. step9_prune 의 보관 기간을 줄이거나 "
            "외부 DB(Turso 등)로 옮길 때다."
        )
    return f"{path}: {kb:,.0f}KB · {lines:,}줄{warn}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DB ↔ SQL 텍스트 덤프")
    parser.add_argument("action", choices=["export", "import", "size"])
    parser.add_argument("--db", default=None)
    parser.add_argument("--sql", default=None)
    args = parser.parse_args(argv)

    if args.action == "export":
        path = export(args.db, args.sql)
        print(f"[dump] 내보냄 {size_report(path)}", file=sys.stderr)
    elif args.action == "import":
        path = restore(args.sql, args.db)
        print(f"[dump] 복원 {path}", file=sys.stderr)
    else:
        print(size_report(Path(args.sql) if args.sql else None), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
