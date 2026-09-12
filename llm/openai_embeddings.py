"""OpenAI 임베딩 어댑터.

임베딩은 batch를 쓰지 않는다 — 클러스터링 직전에 즉시 필요하고, 원래 싸다.
models.yaml 에서 mode: sync 로 명시된 유일한 역할이다.

dimensions 는 models.yaml 에서 읽는다. 512로 잘라 int8 양자화하는 쪽은
pipeline/step3_embed.py 가 담당한다 — 여기는 원본 float 벡터만 돌려준다.
"""
from __future__ import annotations

import os
from typing import Sequence

from .base import EmbedResult, LLMClient, Usage


class OpenAIEmbeddings(LLMClient):
    provider = "openai"

    def __init__(self, role_spec: dict):
        super().__init__(role_spec)
        self._client = None

    def _sdk(self):
        if self._client is None:
            try:
                import openai  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "openai SDK가 없다. pip install openai 하거나 --dry-run 을 써라."
                ) from exc
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError(
                    "OPENAI_API_KEY 가 없다. --dry-run 을 쓰거나 키를 넣어라."
                )
            self._client = openai.OpenAI()
        return self._client

    @property
    def dimensions(self) -> int:
        return int(self.spec.get("dimensions") or 0)

    def embed(self, texts: Sequence[str]) -> EmbedResult:
        if not texts:
            return EmbedResult(vectors=[], model=self.model, usage=Usage())
        client = self._sdk()
        kwargs = {"model": self.model, "input": list(texts)}
        if self.dimensions:
            # text-embedding-3-* 는 서버 쪽에서 차원 축소를 지원한다.
            # 잘라 쓰는 것보다 품질이 낫고 전송량도 준다.
            kwargs["dimensions"] = self.dimensions
        resp = client.embeddings.create(**kwargs)
        # data 는 index 순서가 보장되지만, 명시적으로 정렬해 두는 편이 안전하다.
        rows = sorted(resp.data, key=lambda d: d.index)
        return EmbedResult(
            vectors=[list(d.embedding) for d in rows],
            model=self.model,
            usage=Usage(
                input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                output_tokens=0,
                batch=False,
            ),
        )
