"""step4 — 같은 사건을 묶는다.

여기서 결정되는 사건 수가 곧 LLM 호출 수다. 700건 → 약 180개 사건.
클러스터가 잘게 쪼개지면 비용이 오르고, 뭉뚱그려지면 사건이 뭉개진다.

모든 임계값은 config/pipeline.yaml 에서 읽는다. 코드에 숫자를 박지 않는다.

    python -m pipeline.step4_cluster --report   ← 사건별 기사 제목을 눈으로 본다
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

import config
from store.db import connect

from .common import EXIT_FAIL, EXIT_OK, base_parser, log, table, utc_today
from .step3_embed import dequantize_int8

ROLE = "embedding"


def build_parser():
    p = base_parser("임베딩을 클러스터링해 사건을 만든다")
    p.add_argument(
        "--recluster",
        action="store_true",
        help="그날 사건을 지우고 처음부터 다시 묶는다 (임계값 튜닝용)",
    )
    p.add_argument(
        "--method",
        choices=["auto", "hdbscan", "threshold"],
        default="auto",
        help="auto 는 HDBSCAN 이 있으면 쓰고 없으면 임계값 연결로 떨어진다",
    )
    return p


# ── 클러스터링 ──────────────────────────────────────────────────────────


def cosine_matrix(vectors):
    import numpy as np

    mat = np.asarray(vectors, dtype="float32")
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = mat / norms
    return unit @ unit.T


def cluster_hdbscan(vectors, cfg) -> list[int]:
    """코사인 거리 기반 HDBSCAN. 노이즈는 -1 로 나온다.

    sklearn 의 epsilon 경로(`traverse_upwards`)는 응축 트리가 루트까지 올라가는
    데이터에서 TypeError 로 죽는다. 크론이 거기서 멈추면 그날 사건이 통째로
    비므로, epsilon 없이 한 번 더 시도한 뒤 그래도 안 되면 호출부가
    임계값 방식으로 떨어지게 예외를 올린다.
    """
    import numpy as np
    from sklearn.cluster import HDBSCAN

    distance = 1.0 - cosine_matrix(vectors)
    np.fill_diagonal(distance, 0.0)
    distance = np.clip(distance, 0.0, None).astype("float64")

    epsilon = float(cfg.get("cluster_selection_epsilon", 0.0))
    for attempt_epsilon in ([epsilon, 0.0] if epsilon > 0 else [0.0]):
        model = HDBSCAN(
            min_cluster_size=int(cfg.get("min_cluster_size", 2)),
            min_samples=int(cfg.get("min_samples", 1)),
            cluster_selection_epsilon=attempt_epsilon,
            metric="precomputed",
            copy=True,
        )
        try:
            return [int(v) for v in model.fit_predict(distance)]
        except (TypeError, ValueError) as exc:
            log(
                f"[step4] HDBSCAN 실패 (epsilon={attempt_epsilon}): {exc}"
            )
    raise RuntimeError("HDBSCAN 이 이 데이터에서 실패했다")


def cluster_threshold(vectors, cfg) -> list[int]:
    """유사도 임계값 이상을 잇는 연결 요소. HDBSCAN 이 없을 때의 폴백.

    단순하지만 예측 가능하다 — dry-run 과 테스트가 무거운 의존성 없이 돌아간다.
    """
    threshold = float(cfg.get("similarity_threshold", 0.82))
    sim = cosine_matrix(vectors)
    n = len(vectors)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i][j] >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    groups: dict[int, int] = {}
    labels = []
    min_size = int(cfg.get("min_cluster_size", 2))
    counts = Counter(find(i) for i in range(n))
    for i in range(n):
        root = find(i)
        if counts[root] < min_size:
            labels.append(-1)          # 혼자 뜬 기사는 노이즈로 둔다
            continue
        labels.append(groups.setdefault(root, len(groups)))
    return labels


def assign_labels(vectors, cfg, method: str = "auto") -> tuple[list[int], str]:
    if method == "threshold":
        return cluster_threshold(vectors, cfg), "threshold"
    try:
        return cluster_hdbscan(vectors, cfg), "hdbscan"
    except Exception as exc:  # noqa: BLE001 — 미설치든 내부 버그든 사건은 나와야 한다
        if method == "hdbscan":
            raise
        log(f"[step4] HDBSCAN 을 쓸 수 없어 임계값 연결로 떨어진다: {exc}")
        return cluster_threshold(vectors, cfg), "threshold"


# ── 사건 구성 ───────────────────────────────────────────────────────────


def primary_country(rows) -> str:
    """소속 기사의 country_fips 최빈값. 동률이면 매체 수가 많은 쪽."""
    counts = Counter(r["country_fips"] for r in rows if r["country_fips"])
    if not counts:
        return ""
    top = max(counts.values())
    tied = [fips for fips, n in counts.items() if n == top]
    if len(tied) == 1:
        return tied[0]
    domains = {
        fips: len({r["domain"] for r in rows if r["country_fips"] == fips})
        for fips in tied
    }
    return max(sorted(tied), key=lambda f: domains[f])


def representatives(rows, cfg) -> list[str]:
    """대표 기사. 매체 다양성이 먼저다 — 같은 매체 2건이 대표로 뽑히면
    '여러 매체가 보도한 사건' 이라는 신호가 왜곡된다."""
    limit = int(cfg.get("max_representatives", 3))
    one_per_domain = bool(cfg.get("representative_one_per_domain", True))

    ordered = sorted(
        rows,
        key=lambda r: (
            0 if (r["lead"] or "").strip() else 1,   # 리드가 있는 쪽을 먼저
            -len(r["title"] or ""),
            r["published_at"] or "",
        ),
    )
    picked: list = []
    used_domains: set[str] = set()
    for row in ordered:
        if one_per_domain and row["domain"] in used_domains:
            continue
        picked.append(row["id"])
        used_domains.add(row["domain"])
        if len(picked) >= limit:
            break
    if len(picked) < limit and one_per_domain:
        # 매체가 부족하면 그때는 같은 매체라도 채운다
        for row in ordered:
            if row["id"] not in picked:
                picked.append(row["id"])
            if len(picked) >= limit:
                break
    return picked


def run(args) -> int:
    date = args.date or utc_today()
    db = connect(args.db)
    cfg = config.pipeline().get("cluster") or {}
    model = config.role(ROLE)["model"]

    if args.recluster and not args.dry_run:
        removed = db.reset_clustering(date)
        log(f"[step4] --recluster: 사건 {removed}개를 지우고 다시 묶는다")

    rows = db.articles_by_status("embedded", args.limit)
    embeddings = {
        r["article_id"]: dequantize_int8(r["vector"])
        for r in db.embeddings_for(model, [r["id"] for r in rows])
    }
    rows = [r for r in rows if r["id"] in embeddings]
    if args.limit:
        rows = rows[: args.limit]

    log(f"[step4] {date} · 기사 {len(rows)}건 · 모델 {model}")
    if len(rows) < 2:
        log("[step4] 클러스터링할 기사가 부족하다")
        db.close()
        return EXIT_OK

    labels, method = assign_labels([embeddings[r["id"]] for r in rows], cfg, args.method)
    grouped: dict[int, list] = defaultdict(list)
    for row, label in zip(rows, labels):
        grouped[label].append(row)

    noise = len(grouped.pop(-1, []))
    log(
        f"[step4] {method} · 사건 {len(grouped)}개 · 노이즈 {noise}건 "
        f"· 평균 {(len(rows) - noise) / len(grouped):.1f}건/사건"
        if grouped
        else f"[step4] {method} · 사건 0개 · 노이즈 {noise}건"
    )

    # 이어붙일 때 기존 사건 번호를 덮어쓰지 않게 다음 번호부터 시작한다.
    # 재실행이 조용히 다른 사건을 지워버리는 사고를 막는다.
    start_index = db.next_event_index(date)
    events = []
    for i, (_, members) in enumerate(
        sorted(grouped.items(), key=lambda kv: -len(kv[1])), start=start_index
    ):
        event_id = f"evt_{date.replace('-', '')}_{i:04d}"
        reps = representatives(members, cfg)
        event = {
            "id": event_id,
            "date": date,
            "primary_country": primary_country(members),
            "article_count": len(members),
            "source_count": len({m["domain"] for m in members}),
            "representatives": json.dumps(reps, ensure_ascii=False),
        }
        events.append((event, members))
        if not args.dry_run:
            db.insert_event(event)
            for member in members:
                db.assign_event(member["id"], event_id)

    if args.dry_run:
        log(f"[step4] dry-run — 사건 {len(events)}개 생성 예정, DB에 쓰지 않는다")
    else:
        db.commit()
        log(f"[step4] events {len(events)}개 저장")

    if args.report:
        print(report(events, date, noise))

    db.close()
    return EXIT_OK


def report(events, date: str, noise: int) -> str:
    """클러스터 품질은 눈으로 봐야 한다. 사건별 기사 제목을 그대로 낸다."""
    out = [f"사건 클러스터 ({date}) — {len(events)}개, 노이즈 {noise}건", ""]
    for event, members in events:
        country = config.by_fips(event["primary_country"])
        out.append(
            f"■ {event['id']}  [{country.name_ko if country else event['primary_country']}]"
            f"  기사 {event['article_count']}건 / 매체 {event['source_count']}곳"
        )
        reps = set(json.loads(event["representatives"]))
        for member in members:
            mark = "★" if member["id"] in reps else " "
            title = (member["title"] or "")[:88]
            out.append(f"   {mark} [{member['domain'][:24]:<24}] {title}")
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[step4] 실패: {type(exc).__name__}: {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
