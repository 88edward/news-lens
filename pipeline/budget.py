"""수집 예산 배분.

하루 총량을 상위 N개국에 가중치 비례로 나누되 floor 와 cap 을 둘 다 건다.
  - floor 가 없으면 작은 나라는 영원히 0건이다.
  - cap 이 없으면 미국이 예산의 절반을 먹는다.

클램프된 몫은 버리지 않고 남은 나라들에 다시 비례 배분한다. 한 번만 클램프하면
총합이 목표에서 크게 벗어난다 — 수렴할 때까지 반복해야 한다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class Allocation:
    quotas: dict[str, int]
    warnings: list[str]

    @property
    def total(self) -> int:
        return sum(self.quotas.values())


def allocate(
    weights: list[tuple[str, float]],
    *,
    total: int,
    floor: int,
    cap: int,
    top_n: int | None = None,
) -> Allocation:
    """(fips, weight) 목록 → fips 별 정수 할당량.

    weights 는 weight 내림차순이라고 가정하지 않는다 — 여기서 정렬한다.
    """
    rows = sorted(weights, key=lambda r: r[1], reverse=True)
    if top_n:
        rows = rows[:top_n]
    warnings: list[str] = []
    if not rows:
        return Allocation({}, ["배분할 국가가 없다 — step0을 먼저 돌려라"])

    n = len(rows)
    if floor > cap:
        raise ValueError(f"floor({floor}) 가 cap({cap}) 보다 크다")

    # 실현 가능성부터 확인한다. 조용히 어긋나면 나중에 원인을 못 찾는다.
    if n * floor > total:
        warnings.append(
            f"floor {floor} × {n}개국 = {n * floor} 이 총량 {total} 보다 크다. "
            f"총량을 늘리거나 top_n_countries 를 줄여라. 균등 배분으로 떨어진다."
        )
        base = total // n
        rest = total - base * n
        quotas = {fips: base + (1 if i < rest else 0) for i, (fips, _) in enumerate(rows)}
        return Allocation(quotas, warnings)

    if n * cap < total:
        warnings.append(
            f"cap {cap} × {n}개국 = {n * cap} 이 총량 {total} 보다 작다. "
            f"{total - n * cap}건은 배분되지 않는다."
        )

    fixed: dict[str, int] = {}
    free = {fips: max(0.0, w) for fips, w in rows}
    remaining = float(total)

    # 클램프 → 재배분을 수렴할 때까지 반복
    for _ in range(n + 2):
        if not free:
            break
        weight_sum = sum(free.values())
        if weight_sum <= 0:
            share = remaining / len(free)
            provisional = {fips: share for fips in free}
        else:
            provisional = {
                fips: remaining * w / weight_sum for fips, w in free.items()
            }

        newly: dict[str, int] = {}
        for fips, value in provisional.items():
            if value > cap:
                newly[fips] = cap
            elif value < floor:
                newly[fips] = floor
        if not newly:
            break
        for fips, value in newly.items():
            fixed[fips] = value
            free.pop(fips, None)
            remaining -= value

        if remaining < 0:
            # cap 만으로 총량을 넘겼다 — 남은 나라는 전부 floor 로 둔다
            remaining = 0.0

    # 남은 나라들을 최대 잉여법(largest remainder)으로 정수화한다
    if free:
        weight_sum = sum(free.values())
        raw = {
            fips: (remaining * w / weight_sum if weight_sum > 0 else remaining / len(free))
            for fips, w in free.items()
        }
        floored = {fips: int(v) for fips, v in raw.items()}
        short = int(round(remaining)) - sum(floored.values())
        order = sorted(raw, key=lambda f: raw[f] - floored[f], reverse=True)
        for i in range(max(0, short)):
            floored[order[i % len(order)]] += 1
        fixed.update(floored)

    quotas = {fips: int(fixed.get(fips, 0)) for fips, _ in rows}

    # 반올림 오차 보정: 총량과 정확히 맞춘다 (cap/floor 는 계속 지킨다)
    diff = total - sum(quotas.values())
    if diff != 0:
        adjustable = [
            fips
            for fips, _ in rows
            if (diff > 0 and quotas[fips] < cap) or (diff < 0 and quotas[fips] > floor)
        ]
        step = 1 if diff > 0 else -1
        i = 0
        while diff != 0 and adjustable:
            fips = adjustable[i % len(adjustable)]
            if (step > 0 and quotas[fips] < cap) or (step < 0 and quotas[fips] > floor):
                quotas[fips] += step
                diff -= step
            i += 1
            if i > len(adjustable) * (cap - floor + 2):
                break  # 더 이상 옮길 곳이 없다

    for w in warnings:
        print(f"[budget] {w}", file=sys.stderr)
    return Allocation(quotas, warnings)


def split_queries(quota: int, maxrecords: int, themes: list[str]) -> list[str | None]:
    """할당량이 쿼리 상한을 넘으면 테마별로 쪼갠다.

    GDELT DOC 2.0 은 쿼리당 최대 250건이다. 한 국가에 300건을 배정해 놓고
    쿼리를 하나만 던지면 조용히 250건만 들어온다.

    돌려주는 값은 테마 목록이며, None 은 '테마 제한 없는 쿼리' 를 뜻한다.
    """
    if quota <= maxrecords:
        return [None]
    need = -(-quota // maxrecords)  # 올림 나눗셈
    if not themes:
        print(
            f"[budget] 할당 {quota} 건이 쿼리 상한 {maxrecords} 을 넘는데 "
            "sources.yaml 의 gdelt.themes_for_split 이 비어 있다. "
            f"{maxrecords}건만 들어온다.",
            file=sys.stderr,
        )
        return [None]
    return list(themes[:need]) if need <= len(themes) else list(themes)
