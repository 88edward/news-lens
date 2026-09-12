"""Gemini 임베딩 어댑터 테스트.

실제 API 는 부르지 않는다. SDK 를 가짜로 갈아 끼워 어댑터의 계약만 본다.

여기서 지키려는 것: gemini-embedding-2 에 문자열 리스트를 그대로 넘기면
여러 입력이 **하나로 합쳐진 임베딩 1개**로 돌아온다. 그대로 쓰면 모든 기사가
같은 벡터가 되어 클러스터링이 조용히 무너진다 — 에러는 나지 않고
"사건이 이상하게 묶인다"로만 나타나서 원인을 찾기 어렵다.
"""
from __future__ import annotations

import sys
import types as pytypes

import pytest

import config
from llm.gemini_client import GeminiClient


class FakeEmbedding:
    def __init__(self, values, tokens=7):
        self.values = values
        self.statistics = pytypes.SimpleNamespace(token_count=tokens, truncated=False)


class FakeResponse:
    def __init__(self, embeddings):
        self.embeddings = embeddings


@pytest.fixture
def fake_sdk(monkeypatch):
    """google.genai 를 흉내 낸다. 호출 인자를 기록해 검증에 쓴다."""
    calls = {}

    class FakeModels:
        def embed_content(self, *, model, contents, config=None):
            calls["model"] = model
            calls["contents"] = contents
            calls["config"] = config
            n = calls.get("force_count", len(contents))
            return FakeResponse([FakeEmbedding([0.1] * 768) for _ in range(n)])

    class FakeClient:
        models = FakeModels()

    genai = pytypes.ModuleType("google.genai")
    genai.Client = lambda **kw: FakeClient()

    types_mod = pytypes.ModuleType("google.genai.types")

    class Part:
        def __init__(self, text=""):
            self.text = text

    class Content:
        def __init__(self, parts=None):
            self.parts = parts or []

    class EmbedContentConfig:
        def __init__(self, output_dimensionality=None):
            self.output_dimensionality = output_dimensionality

    types_mod.Part = Part
    types_mod.Content = Content
    types_mod.EmbedContentConfig = EmbedContentConfig

    google = pytypes.ModuleType("google")
    google.genai = genai
    genai.types = types_mod

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return calls


def client_for(role="embedding"):
    return GeminiClient(config.role(role))


def test_텍스트마다_벡터_하나씩_나온다(config_dir, fake_sdk):
    result = client_for().embed(["첫 번째 기사", "두 번째 기사", "세 번째 기사"])
    assert len(result.vectors) == 3
    assert all(len(v) == 768 for v in result.vectors)


def test_각_텍스트를_Content로_감싼다(config_dir, fake_sdk):
    """문자열 리스트를 그대로 넘기면 하나로 합쳐진 임베딩이 돌아온다."""
    client_for().embed(["A", "B"])
    contents = fake_sdk["contents"]
    assert len(contents) == 2
    assert all(hasattr(c, "parts") for c in contents), "Content 로 감싸지 않았다"
    assert [c.parts[0].text for c in contents] == ["A", "B"]


def test_개수가_안_맞으면_즉시_터진다(config_dir, fake_sdk):
    """합쳐진 응답을 조용히 받아들이면 모든 기사가 같은 벡터가 된다."""
    fake_sdk["force_count"] = 1
    with pytest.raises(RuntimeError, match="임베딩 개수가 맞지 않는다"):
        client_for().embed(["A", "B", "C"])


def test_차원을_설정에서_읽어_넘긴다(config_dir, fake_sdk):
    client_for().embed(["A"])
    dims = int(config.role("embedding")["dimensions"])
    assert fake_sdk["config"].output_dimensionality == dims


def test_모델_ID를_설정에서_읽는다(config_dir, fake_sdk):
    client_for().embed(["A"])
    assert fake_sdk["model"] == config.role("embedding")["model"]


def test_토큰_사용량을_집계한다(config_dir, fake_sdk):
    """비용 원장이 이 값에 의존한다."""
    result = client_for().embed(["A", "B", "C"])
    assert result.usage.input_tokens == 21      # 7 × 3
    assert result.usage.batch is False


def test_빈_입력은_API를_부르지_않는다(config_dir, fake_sdk):
    result = client_for().embed([])
    assert result.vectors == []
    assert "model" not in fake_sdk


def test_키가_없으면_친절하게_실패한다(config_dir, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        client_for().embed(["A"])


def test_기본_설정이_Gemini_하나로_돈다(config_dir):
    """가입할 곳이 하나여야 한다는 게 이 구성의 요점이다."""
    providers = {config.role(r)["provider"] for r in config.role_names()}
    assert providers == {"gemini"}
