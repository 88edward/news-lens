"""오프라인 어댑터. 네트워크도 API 키도 쓰지 않는다.

두 가지 용도가 있다.
  1. `config/models.yaml` 의 provider 를 `fake` 로 두면 키 없이 전체 파이프라인이 돈다.
     README 의 "fixtures 로 전체 돌리기" 가 이것에 의존한다.
  2. 테스트가 이걸 그대로 쓴다 (tests/fake_provider.py).

실제 운영 설정에서 provider 가 fake 로 남아 있으면 사이트에 가짜 분석이 올라간다.
그래서 호출할 때마다 stderr 에 경고를 찍는다.
"""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Iterable, Sequence

from .base import (
    BatchItemResult,
    BatchRequest,
    BatchResults,
    BatchStatus,
    Completion,
    EmbedResult,
    LLMClient,
    Usage,
)

_warned: set[str] = set()


def _warn_once(role: str) -> None:
    if role in _warned:
        return
    _warned.add(role)
    print(
        f"[llm] 경고: 역할 '{role}' 이 fake provider 로 돌고 있다. "
        "결과는 가짜다 — 운영 설정이 아닌지 확인해라.",
        file=sys.stderr,
    )


def deterministic_vector(text: str, dims: int) -> list[float]:
    """같은 텍스트 → 같은 벡터. 비슷한 텍스트 → 비슷한 벡터.

    단어 해시를 차원에 흩뿌린다. 진짜 임베딩은 아니지만 어휘가 겹치는 기사끼리
    가까워지므로, 클러스터링 단계를 오프라인에서 의미 있게 시험할 수 있다.
    """
    vec = [0.0] * dims
    for word in text.lower().split():
        h = int(hashlib.sha1(word.encode("utf-8")).hexdigest(), 16)
        vec[h % dims] += 1.0
        vec[(h >> 8) % dims] += 0.5
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class FakeClient(LLMClient):
    """embed · complete · batch 를 전부 흉내 낸다."""

    provider = "fake"

    #: 테스트가 조작하는 클래스 레벨 상태
    pending_batches: set[str] = set()
    batch_store: dict[str, list[BatchRequest]] = {}
    responder = None  # callable(BatchRequest) -> str
    submit_count: int = 0

    # ── embed ─────────────────────────────────────────────────────────

    def embed(self, texts: Sequence[str]) -> EmbedResult:
        _warn_once(self.role or "embedding")
        dims = int(self.spec.get("dimensions") or 512)
        return EmbedResult(
            vectors=[deterministic_vector(t, dims) for t in texts],
            model=self.model,
            usage=Usage(input_tokens=sum(max(1, len(t) // 4) for t in texts)),
        )

    # ── sync ──────────────────────────────────────────────────────────

    def complete(
        self, system: str, user: str, max_output_tokens: int | None = None
    ) -> Completion:
        _warn_once(self.role)
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
        _warn_once(self.role)
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

    # ── 응답 ──────────────────────────────────────────────────────────

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
        _warned.clear()
