"""비용 계산과 상한 강제 테스트.

상한은 경고가 아니라 중단이어야 한다. 그 점을 여기서 고정한다.
"""
from __future__ import annotations

import pytest

import config
from llm import cost
from llm.base import Usage


def test_동기_호출_비용이_단가와_맞는다(config_dir):
    """단가는 models.yaml 에서 온다. 여기에 숫자를 박으면 모델을 바꿀 때 깨진다."""
    spec = config.role("daily_synthesis")
    expected = spec["price_per_mtok_in"] + spec["price_per_mtok_out"]
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, batch=False)
    assert cost.price_of("daily_synthesis", usage) == pytest.approx(expected)


def test_batch는_반값이다(config_dir):
    sync = Usage(input_tokens=1_000_000, output_tokens=1_000_000, batch=False)
    batch = Usage(input_tokens=1_000_000, output_tokens=1_000_000, batch=True)
    assert cost.price_of("event_analysis", batch) == pytest.approx(
        cost.price_of("event_analysis", sync) / 2
    )


def test_캐시_읽기는_입력의_10퍼센트로_계산된다(config_dir):
    plain = Usage(input_tokens=1_000_000, batch=False)
    cached = Usage(input_tokens=0, cached_input_tokens=1_000_000, batch=False)
    assert cost.price_of("event_analysis", cached) == pytest.approx(
        cost.price_of("event_analysis", plain) * cost.CACHE_READ_MULTIPLIER
    )


def test_월_비용이_예산_안에_들어온다(config_dir):
    """사건 분석 하루 180건 × 30일 = 5,400 호출.

    CLAUDE.md 가 정한 LLM 예산은 월 $25 다. 기본 설정이 그 안에 들어와야 한다.
    단가표나 호출 수 가정이 틀어지면 여기서 걸린다.
    """
    monthly = cost.estimate(
        "event_analysis", in_tokens=3_000, out_tokens=700, n=180 * 30
    )
    assert 0 < monthly < 25.0, f"월 ${monthly:.2f} 는 예산 $25 를 넘는다"


def test_일일_상한이_실제_하루치를_감당한다(config_dir):
    """상한이 너무 낮으면 매일 step5 가 중단된다 — 조용히 사이트가 안 갱신된다."""
    daily = cost.estimate("event_analysis", in_tokens=3_000, out_tokens=700, n=180)
    daily += cost.estimate("embedding", in_tokens=60, out_tokens=0, n=700)
    daily += cost.estimate("daily_synthesis", in_tokens=8_000, out_tokens=4_000, n=1)
    assert daily < cost.max_daily_cost_usd(), (
        f"하루 예상 ${daily:.4f} 가 상한 ${cost.max_daily_cost_usd()} 를 넘는다"
    )


def test_상한을_넘길_호출은_중단된다(config_dir):
    config_dir.patch("pipeline", lambda d: d["cost"].update({"max_daily_cost_usd": 0.05}))
    ledger = cost.Ledger()
    with pytest.raises(cost.CostLimitExceeded, match="상한"):
        ledger.preflight("event_analysis", in_tokens=3_000, out_tokens=700, n=180)
    assert ledger.spent_usd == 0.0  # 중단했으므로 쓰지 않았다


def test_상한_안이면_통과하고_예상치를_돌려준다(config_dir):
    ledger = cost.Ledger()
    projected = ledger.preflight("event_analysis", in_tokens=3_000, out_tokens=700, n=10)
    assert projected > 0
    assert ledger.spent_usd == 0.0


def test_누적이_상한을_넘으면_기록_시점에_터진다(config_dir):
    config_dir.patch("pipeline", lambda d: d["cost"].update({"max_daily_cost_usd": 0.01}))
    ledger = cost.Ledger()
    with pytest.raises(cost.CostLimitExceeded, match="넘었다"):
        for _ in range(50):
            ledger.record(
                "event_analysis", Usage(input_tokens=100_000, output_tokens=5_000, batch=True)
            )
    assert ledger.spent_usd > ledger.limit


def test_역할별로_누적된다(config_dir):
    ledger = cost.Ledger()
    ledger.record("event_analysis", Usage(input_tokens=1000, output_tokens=100, batch=True))
    ledger.record("event_analysis", Usage(input_tokens=1000, output_tokens=100, batch=True))
    ledger.record("daily_synthesis", Usage(input_tokens=5000, output_tokens=2000, batch=True))
    assert ledger.calls == {"event_analysis": 2, "daily_synthesis": 1}
    assert ledger.spent_usd == pytest.approx(sum(ledger.by_role.values()))
    assert "event_analysis" in ledger.report()


def test_상한이_없으면_설정_오류다(config_dir):
    config_dir.patch("pipeline", lambda d: d["cost"].pop("max_daily_cost_usd"))
    with pytest.raises(config.ConfigError, match="상한 없는 실행은 금지"):
        cost.max_daily_cost_usd()


def test_모델을_바꾸면_비용도_따라_바뀐다(config_dir):
    """품질안(Anthropic Haiku)으로 올리면 비용이 따라 올라야 한다."""
    before = cost.estimate("event_analysis", in_tokens=3_000, out_tokens=700, n=5400)
    config_dir.patch(
        "models",
        lambda d: d["roles"]["event_analysis"].update(
            {
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "price_per_mtok_in": 1.0,
                "price_per_mtok_out": 5.0,
            }
        ),
    )
    after = cost.estimate("event_analysis", in_tokens=3_000, out_tokens=700, n=5400)
    assert after > before * 5  # 기본값(저가안)보다 확실히 비싸야 한다


def test_원장은_db에_기록을_넘긴다(config_dir):
    calls = []

    class FakeDB:
        def spent_today(self, day):
            return 0.25

        def record_run(self, **kw):
            calls.append(kw)

    ledger = cost.Ledger(db=FakeDB())
    ledger.load_from_db()
    assert ledger.spent_usd == 0.25  # 다른 실행이 쓴 금액을 이어받는다

    ledger.record("event_analysis", Usage(input_tokens=100, output_tokens=10, batch=True))
    assert calls and calls[0]["role"] == "event_analysis"
    assert calls[0]["cost_usd"] > 0
