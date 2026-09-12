"""step3 — 임베딩.

제목 + 리드만 넣는다. 본문 전체를 넣으면 토큰이 10배가 되고, 클러스터링 품질은
오히려 나빠진다 (긴 본문의 공통 상용구가 유사도를 끌어올린다).

embeddings 테이블에 model 을 남긴다. 모델을 바꾸면 기존 벡터와 거리 계산이
호환되지 않으므로, 어떤 모델로 만든 벡터인지 반드시 남아야 한다.
"""
from __future__ import annotations

import struct
import sys

import config
from llm import cost
from llm.registry import get_client
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, utc_today

ROLE = "embedding"


def build_parser():
    p = base_parser("제목+리드를 임베딩해 embeddings 테이블에 넣는다")
    p.add_argument(
        "--reembed", action="store_true", help="이미 임베딩된 기사도 다시 만든다"
    )
    return p


def text_for(row) -> str:
    fields = (config.pipeline().get("embed") or {}).get("text_fields") or ["title", "lead"]
    parts = [str(row[f] or "").strip() for f in fields if f in row.keys()]
    return "\n".join(p for p in parts if p)


def quantize_int8(vector: list[float]) -> bytes:
    """[-1, 1] 범위 float → int8. 벡터당 512바이트가 된다.

    코사인 유사도는 스케일에 불변이므로, 정규화 후 양자화해도 클러스터링 품질에
    영향이 거의 없다. 대신 저장량이 4배 줄어 무료 티어 안에 들어온다.
    """
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    return bytes(
        (max(-127, min(127, int(round(v / norm * 127)))) & 0xFF) for v in vector
    )


def dequantize_int8(blob: bytes) -> list[float]:
    values = struct.unpack(f"{len(blob)}b", blob)
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)

    spec = config.role(ROLE)
    model = spec["model"]
    embed_cfg = config.pipeline().get("embed") or {}
    dims = int(spec.get("dimensions") or embed_cfg.get("dimensions") or 512)
    batch_size = int(embed_cfg.get("batch_size", 128))
    quantize = embed_cfg.get("quantize", "int8")

    rows = db.articles_by_status("filtered", args.limit)
    if not args.reembed:
        done = db.embedded_ids(model)   # 같은 모델로 만든 것만 건너뛴다
        rows = [r for r in rows if r["id"] not in done]
    if args.limit:
        rows = rows[: args.limit]

    log(f"[step3] {date} · {len(rows)}건 · {model} · {dims}차원 {quantize}")
    if not rows:
        db.close()
        return EXIT_OK

    texts = [text_for(r) for r in rows]
    ledger = cost.ledger(db=db)
    ledger.preflight(
        ROLE,
        in_tokens=sum(max(1, len(t) // 4) for t in texts) // max(1, len(texts)),
        out_tokens=0,
        n=len(texts),
    )

    if args.dry_run:
        log(f"[step3] dry-run — {len(rows)}건 임베딩 예정, API 를 부르지 않는다")
        db.close()
        return EXIT_OK

    client = get_client(ROLE)
    written = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        result = client.embed([text_for(r) for r in chunk])
        ledger.record(ROLE, result.usage, note=f"{len(chunk)}건")
        for row, vector in zip(chunk, result.vectors):
            trimmed = vector[:dims]
            db.save_embedding(
                row["id"], model, len(trimmed), quantize, quantize_int8(trimmed)
            )
            db.set_article_status(row["id"], "embedded")
            written += 1
        db.commit()
        log(f"[step3] {written}/{len(rows)}")

    log(f"[step3] 완료 {written}건 · 비용 ${ledger.by_role.get(ROLE, 0):.4f}")
    db.close()
    return EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step3] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
