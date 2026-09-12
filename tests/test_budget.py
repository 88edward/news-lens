"""예산 배분 테스트.

완료 조건: 가중치가 극단적으로 치우쳐도 (한 나라 weight 0.9) cap 때문에
200건을 넘지 않고, 하위 국가도 floor 10건을 받는다.
"""
from __future__ import annotations

import pytest

from pipeline.budget import allocate, split_queries


def make_weights(n: int, top: float | None = None) -> list[tuple[str, float]]:
    rows = [(f"C{i:02d}", 1.0 / (i + 1)) for i in range(n)]
    if top is not None:
        rest = (1.0 - top) / (n - 1)
        rows = [("C00", top)] + [(f"C{i:02d}", rest) for i in range(1, n)]
    return rows


def test_기본_배분은_총량을_정확히_맞춘다():
    alloc = allocate(make_weights(25), total=1000, floor=10, cap=200)
    assert alloc.total == 1000
    assert len(alloc.quotas) == 25


def test_극단적_치우침에도_cap과_floor를_지킨다():
    """한 나라가 weight 0.9 여도 200건을 넘지 않고, 나머지도 10건은 받는다."""
    alloc = allocate(make_weights(25, top=0.9), total=1000, floor=10, cap=200)
    assert max(alloc.quotas.values()) <= 200
    assert min(alloc.quotas.values()) >= 10
    assert alloc.quotas["C00"] == 200      # 상한에 딱 붙는다
    assert alloc.total == 1000             # 클램프된 몫이 버려지지 않는다


def test_cap이_없으면_미국이_절반을_먹는다는_것을_보인다():
    """cap 을 크게 두면 실제로 그렇게 된다 — cap 이 있어야 하는 이유."""
    alloc = allocate(make_weights(25, top=0.9), total=1000, floor=0, cap=10_000)
    assert alloc.quotas["C00"] > 500


def test_floor가_없으면_꼬리_국가는_0건이_된다():
    """실제 가중치 분포는 급격히 꺾인다 — 꼬리는 1건도 못 받는다.

    상위를 뺀 나머지가 균등한 인공 분포에서는 이 문제가 안 보인다.
    floor 가 필요한 이유는 분포가 가파르기 때문이다.
    """
    steep = [(f"C{i:02d}", 0.5**i) for i in range(25)]
    without_floor = allocate(steep, total=1000, floor=0, cap=200)
    assert min(without_floor.quotas.values()) == 0

    with_floor = allocate(steep, total=1000, floor=10, cap=200)
    assert min(with_floor.quotas.values()) == 10
    assert with_floor.total == 1000


def test_가중치가_전부_0이어도_균등하게_나눈다():
    alloc = allocate([(f"C{i}", 0.0) for i in range(10)], total=1000, floor=10, cap=200)
    assert alloc.total == 1000
    assert all(10 <= v <= 200 for v in alloc.quotas.values())


def test_floor가_총량보다_크면_경고하고_균등배분한다():
    alloc = allocate(make_weights(25), total=100, floor=10, cap=200)
    assert alloc.total == 100
    assert any("floor" in w for w in alloc.warnings)


def test_cap이_총량보다_작으면_경고한다():
    alloc = allocate(make_weights(5), total=1000, floor=10, cap=100)
    assert alloc.total <= 500
    assert any("배분되지 않는다" in w for w in alloc.warnings)


def test_top_n을_넘는_국가는_잘린다():
    alloc = allocate(make_weights(58), total=1000, floor=10, cap=200, top_n=25)
    assert len(alloc.quotas) == 25


def test_배분은_가중치_순서를_뒤집지_않는다():
    weights = dict(make_weights(25))
    alloc = allocate(list(weights.items()), total=1000, floor=10, cap=200)
    ordered = sorted(weights, key=lambda f: weights[f], reverse=True)
    for a, b in zip(ordered, ordered[1:]):
        assert alloc.quotas[a] >= alloc.quotas[b], f"{a} < {b}"


def test_floor가_cap보다_크면_거부한다():
    with pytest.raises(ValueError, match="floor"):
        allocate(make_weights(5), total=1000, floor=300, cap=200)


def test_빈_입력은_경고와_함께_빈_결과():
    alloc = allocate([], total=1000, floor=10, cap=200)
    assert alloc.quotas == {} and alloc.warnings


# ── 쿼리 분할 ───────────────────────────────────────────────────────────


def test_상한_이하면_쿼리를_쪼개지_않는다():
    assert split_queries(200, 250, ["A", "B", "C"]) == [None]


def test_상한을_넘으면_테마로_쪼갠다():
    plan = split_queries(600, 250, ["A", "B", "C", "D"])
    assert len(plan) == 3 and None not in plan


def test_테마가_부족하면_있는_만큼만_쓴다():
    plan = split_queries(2000, 250, ["A", "B"])
    assert plan == ["A", "B"]


def test_테마가_없으면_경고하고_한_쿼리로_간다(capsys):
    assert split_queries(600, 250, []) == [None]
    assert "themes_for_split" in capsys.readouterr().err
