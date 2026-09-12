"""registry 계약 테스트.

완료 조건: models.yaml 의 event_analysis provider 를 anthropic → gemini 로
한 줄 바꿨을 때, 파이썬 코드를 한 줄도 안 고치고 이 테스트가 그대로 통과한다.
"""
from __future__ import annotations

import pytest

import config
from llm import registry
from llm.base import BatchRequest, BatchStatus, LLMClient


def test_역할_이름으로_클라이언트를_얻는다(config_dir):
    client = registry.get_client("event_analysis")
    assert isinstance(client, LLMClient)
    assert client.model == config.role("event_analysis")["model"]
    assert client.is_batch


def test_provider를_바꿔도_같은_인터페이스가_나온다(config_dir):
    """models.yaml 한 줄만 바꾼다. 아래 코드는 그대로다.

    어느 provider 로 출하됐든 통과해야 한다 — 기본값을 여기에 박으면
    models.yaml 을 바꾸는 순간 테스트가 깨지고, 그건 설정 계층이 아니다.
    """
    before = registry.get_client("event_analysis")
    others = [p for p in registry.provider_names() if p != before.provider]
    assert others, "교체 대상 provider 가 하나도 없다"
    target = "gemini" if "gemini" in others else others[0]

    config_dir.patch(
        "models",
        lambda d: d["roles"]["event_analysis"].update(
            {
                "provider": target,
                "model": f"{target}-some-model",
                "price_per_mtok_in": 0.10,
                "price_per_mtok_out": 0.40,
            }
        ),
    )

    after = registry.get_client("event_analysis", fresh=True)
    assert after.provider == target != before.provider
    assert isinstance(after, LLMClient)

    # 인터페이스가 동일한지 — 호출부가 알아야 할 이름이 전부 있는지
    for attr in ("embed", "submit_batch", "fetch_batch", "complete", "is_batch", "model"):
        assert hasattr(after, attr), attr
    assert type(before).__mro__[1] is type(after).__mro__[1] is LLMClient


def test_알려지지_않은_provider는_친절하게_실패한다(config_dir):
    config_dir.patch(
        "models", lambda d: d["roles"]["event_analysis"].update({"provider": "nope"})
    )
    with pytest.raises(registry.ProviderError, match="어댑터가 없다"):
        registry.get_client("event_analysis", fresh=True)


def test_역할을_추가할_때_registry를_고치지_않는다(config_dir, fake_client):
    """models.yaml 에 역할을 새로 적기만 하면 바로 쓸 수 있어야 한다."""
    config_dir.patch(
        "models",
        lambda d: d["roles"].update(
            {
                "headline_rewrite": {
                    "provider": "fake",
                    "model": "fake-model-1",
                    "mode": "sync",
                    "max_output_tokens": 100,
                    "price_per_mtok_in": 1.0,
                    "price_per_mtok_out": 2.0,
                }
            }
        ),
    )
    client = registry.get_client("headline_rewrite")
    assert client.model == "fake-model-1"
    assert client.complete("s", "u").text


def test_없는_역할은_있는_역할을_알려준다(config_dir):
    with pytest.raises(config.ConfigError, match="있는 역할"):
        registry.get_client("nonexistent_role")


def test_mode는_batch나_sync만_허용한다(config_dir):
    config_dir.patch(
        "models", lambda d: d["roles"]["event_analysis"].update({"mode": "streaming"})
    )
    with pytest.raises(config.ConfigError, match="batch 또는 sync"):
        config.role("event_analysis")


def test_모든_역할이_로드된다(config_dir):
    for name in config.role_names():
        spec = config.role(name)
        assert spec["provider"] and spec["model"]
        registry.get_client(name, fresh=True)


def test_fake_provider_batch_왕복(config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["event_analysis"].update({"provider": "fake"})
    )
    client = registry.get_client("event_analysis", fresh=True)
    batch_id = client.submit_batch(
        [BatchRequest(custom_id="evt_1", system="sys", user="body")]
    )

    fake_client.pending_batches.add(batch_id)
    assert client.fetch_batch(batch_id).status is BatchStatus.PENDING

    fake_client.pending_batches.discard(batch_id)
    results = client.fetch_batch(batch_id)
    assert results.status is BatchStatus.ENDED
    assert [i.custom_id for i in results.items] == ["evt_1"]
    assert results.usage.batch is True


def test_빈_배치는_제출하지_않는다(config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["event_analysis"].update({"provider": "fake"})
    )
    client = registry.get_client("event_analysis", fresh=True)
    with pytest.raises(ValueError):
        client.submit_batch([])


def test_지원하지_않는_기능은_역할_이름과_함께_실패한다(config_dir, fake_client):
    """embedding 역할에 batch 를 요구하면 무엇이 문제인지 바로 알 수 있어야 한다."""
    client = registry.get_client("embedding")
    with pytest.raises(NotImplementedError, match="embedding"):
        client.submit_batch([BatchRequest(custom_id="x", system="s", user="u")])
