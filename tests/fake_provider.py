"""테스트용 가짜 provider. 실제 API는 절대 호출하지 않는다.

registry.register() 로 끼워 넣어 쓴다. 진짜 어댑터와 똑같은 인터페이스만 노출하므로,
이걸로 통과하는 테스트는 provider 교체 가능성을 실제로 검증한다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Sequence

from llm.base import (
    BatchItemResult,
    BatchRequest,
    BatchResults,
    BatchStatus,
    Completion,
    LLMClient,
    Usage,
)


def _deterministic_vector(text: str, dims: int) -> list[float]:
    """텍스트가 같으면 같은 벡터. 비슷한 텍스트는 비슷한 벡터가 되도록
    단어 해시를 차원에 흩뿌린다 — 클러스터링 테스트가 의미를 가지려면 필요하다."""
    vec = [0.0] * dims
    for word in text.lower().split():
        h = int(hashlib.sha1(word.encode("utf-8")).hexdigest(), 16)
        vec[h % dims] += 1.0
        vec[(h >> 8) % dims] += 0.5
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class FakeClient(LLMClient):
    """batch·sync·embed 를 전부 흉내 내는 단일 가짜 클라이언트."""

    provider = "fake"

    #: 테스트가 조작하는 클래스 레벨 상태
    pending_batches: set[str] = set()
    batch_store: dict[str, list[BatchRequest]] = {}
    responder = None  # callable(BatchRequest) -> str
    submit_count: int = 0

    def __init__(self, role_spec: dict):
        super().__init__(role_spec)

    # ── embed ─────────────────────────────────────────────────────────

    def embed(self, texts: Sequence[str]):
        from llm.base import EmbedResult

        dims = int(self.spec.get("dimensions") or 512)
        vectors = [_deterministic_vector(t, dims) for t in texts]
        return EmbedResult(
            vectors=vectors,
            model=self.model,
            usage=Usage(input_tokens=sum(max(1, len(t) // 4) for t in texts)),
        )

    # ── sync ──────────────────────────────────────────────────────────

    def complete(
        self, system: str, user: str, max_output_tokens: int | None = None
    ) -> Completion:
        text = self._respond(BatchRequest(custom_id="sync", system=system, user=user))
        return Completion(
            text=text,
            model=self.model,
            usage=Usage(
                input_tokens=max(1, (len(system) + len(user)) // 4),
                output_tokens=max(1, len(text) // 4),
                batch=False,
            ),
        )

    # ── batch ─────────────────────────────────────────────────────────

    def submit_batch(self, requests: Iterable[BatchRequest]) -> str:
        reqs = list(requests)
        if not reqs:
            raise ValueError("빈 배치는 제출하지 않는다")
        type(self).submit_count += 1
        batch_id = f"fake_batch_{type(self).submit_count:03d}"
        type(self).batch_store[batch_id] = reqs
        return batch_id

    def fetch_batch(self, batch_id: str) -> BatchResults:
        if batch_id in type(self).pending_batches:
            return BatchResults(
                batch_id=batch_id, status=BatchStatus.PENDING, model=self.model
            )
        reqs = type(self).batch_store.get(batch_id, [])
        items = []
        for req in reqs:
            text = self._respond(req)
            items.append(
                BatchItemResult(
                    custom_id=req.custom_id,
                    ok=True,
                    text=text,
                    usage=Usage(
                        input_tokens=max(1, (len(req.system) + len(req.user)) // 4),
                        output_tokens=max(1, len(text) // 4),
                        batch=True,
                    ),
                )
            )
        return BatchResults(
            batch_id=batch_id, status=BatchStatus.ENDED, items=items, model=self.model
        )

    # ── 응답 생성 ─────────────────────────────────────────────────────

    def _respond(self, req: BatchRequest) -> str:
        if type(self).responder is not None:
            return type(self).responder(req)
        return json.dumps({"echo": req.custom_id}, ensure_ascii=False)

    @classmethod
    def reset(cls) -> None:
        cls.pending_batches = set()
        cls.batch_store = {}
        cls.responder = None
        cls.submit_count = 0
