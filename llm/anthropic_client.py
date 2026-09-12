"""Anthropic 어댑터.

anthropic SDK를 import 해도 되는 몇 안 되는 파일 중 하나다.
SDK가 설치돼 있지 않아도 이 모듈은 import 되어야 한다 — dry-run 이 돌아야 하므로
SDK import 는 실제 호출 시점까지 미룬다.
"""
from __future__ import annotations

import os
from typing import Iterable

from .base import (
    BatchItemResult,
    BatchRequest,
    BatchResults,
    BatchStatus,
    Completion,
    LLMClient,
    Usage,
)


def estimate_tokens(text: str) -> int:
    """API 키 없이 비용을 가늠하기 위한 거친 추정. 영문 기준 ~4자/토큰."""
    return max(1, len(text) // 4)


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, role_spec: dict):
        super().__init__(role_spec)
        self._client = None

    # ── SDK 지연 로딩 ──────────────────────────────────────────────────

    def _sdk(self):
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415 — 지연 import 는 의도적이다
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "anthropic SDK가 없다. pip install anthropic 하거나 --dry-run 을 써라."
                ) from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY 가 없다. --dry-run 을 쓰거나 키를 넣어라."
                )
            self._client = anthropic.Anthropic()
        return self._client

    @staticmethod
    def _usage(raw, batch: bool) -> Usage:
        return Usage(
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cached_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
            batch=batch,
        )

    @staticmethod
    def _text(message) -> str:
        return "".join(
            b.text for b in message.content if getattr(b, "type", "") == "text"
        )

    # ── 동기 호출 ──────────────────────────────────────────────────────

    def complete(
        self, system: str, user: str, max_output_tokens: int | None = None
    ) -> Completion:
        client = self._sdk()
        msg = client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens or self.max_output_tokens or 4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return Completion(
            text=self._text(msg),
            model=self.model,
            usage=self._usage(msg.usage, batch=False),
            stop_reason=getattr(msg, "stop_reason", None),
        )

    # ── Batch API ─────────────────────────────────────────────────────

    def submit_batch(self, requests: Iterable[BatchRequest]) -> str:
        from anthropic.types.message_create_params import (  # noqa: PLC0415
            MessageCreateParamsNonStreaming,
        )
        from anthropic.types.messages.batch_create_params import (  # noqa: PLC0415
            Request,
        )

        client = self._sdk()
        payload = [
            Request(
                custom_id=req.custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=self.model,
                    max_tokens=req.max_output_tokens or self.max_output_tokens or 4096,
                    system=req.system,
                    messages=[{"role": "user", "content": req.user}],
                ),
            )
            for req in requests
        ]
        if not payload:
            raise ValueError("빈 배치는 제출하지 않는다")
        batch = client.messages.batches.create(requests=payload)
        return batch.id

    def fetch_batch(self, batch_id: str) -> BatchResults:
        client = self._sdk()
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            return BatchResults(
                batch_id=batch_id, status=BatchStatus.PENDING, model=self.model
            )

        items: list[BatchItemResult] = []
        # 결과는 순서가 보장되지 않는다. custom_id 로만 매칭한다.
        for result in client.messages.batches.results(batch_id):
            kind = result.result.type
            if kind == "succeeded":
                msg = result.result.message
                items.append(
                    BatchItemResult(
                        custom_id=result.custom_id,
                        ok=True,
                        text=self._text(msg),
                        usage=self._usage(msg.usage, batch=True),
                    )
                )
            else:
                err = getattr(result.result, "error", None)
                err_type = getattr(err, "type", "") or ""
                items.append(
                    BatchItemResult(
                        custom_id=result.custom_id,
                        ok=False,
                        error=(kind + " " + err_type).strip(),
                        usage=Usage(batch=True),
                    )
                )
        return BatchResults(
            batch_id=batch_id, status=BatchStatus.ENDED, items=items, model=self.model
        )

    # ── 비용 추정용 (호출 전 stderr에 찍는다) ──────────────────────────

    def count_tokens(self, system: str, user: str) -> int:
        """정확한 토큰 수. 키나 SDK가 없으면 대략치로 떨어진다."""
        try:
            client = self._sdk()
            resp = client.messages.count_tokens(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.input_tokens
        except Exception:  # noqa: BLE001 — 추정치 실패는 치명적이지 않다
            return estimate_tokens(system) + estimate_tokens(user)
