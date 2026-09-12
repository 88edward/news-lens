"""LLM 어댑터의 추상 인터페이스.

벤더 SDK는 이 디렉토리 안에서만 import 한다. pipeline/ 은 절대 직접 부르지 않는다.
모든 반환값은 usage(입력·출력 토큰)를 포함한다 — 비용 집계의 유일한 근거다.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence


class BatchStatus(str, Enum):
    PENDING = "pending"
    ENDED = "ended"
    FAILED = "failed"


@dataclass(frozen=True)
class Usage:
    """한 번의 호출이 쓴 토큰. 비용은 llm/cost.py 가 계산한다."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    batch: bool = False

    def __add__(self, other: "Usage") -> "Usage":
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            batch=self.batch or other.batch,
        )


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    model: str
    usage: Usage

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    usage: Usage
    stop_reason: str | None = None


@dataclass(frozen=True)
class BatchRequest:
    """배치에 들어가는 요청 한 건.

    custom_id 는 사건 id 등 호출자가 나중에 결과를 되붙일 키다.
    결과는 순서가 보장되지 않으므로 custom_id 로만 매칭한다.
    """

    custom_id: str
    system: str
    user: str
    max_output_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchItemResult:
    custom_id: str
    ok: bool
    text: str = ""
    error: str | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class BatchResults:
    batch_id: str
    status: BatchStatus
    items: list[BatchItemResult] = field(default_factory=list)
    model: str = ""

    @property
    def pending(self) -> bool:
        return self.status is BatchStatus.PENDING

    @property
    def usage(self) -> Usage:
        total = Usage(batch=True)
        for item in self.items:
            total = total + item.usage
        return total


class LLMClient(abc.ABC):
    """역할 하나에 묶인 클라이언트.

    role_spec 은 config.role(name) 이 돌려준 dict — provider·model·단가·mode 를 갖는다.
    구현체는 모델 ID를 하드코딩하지 않고 self.model 만 쓴다.
    """

    #: 이 클래스가 처리하는 provider 이름. registry가 이걸로 찾는다.
    provider: str = ""

    def __init__(self, role_spec: dict):
        self.spec = dict(role_spec)
        self.role = role_spec.get("role", "")
        self.model = role_spec["model"]
        self.mode = role_spec["mode"]

    # ── 이름만 보고 구현 여부를 판단할 수 있게 기본은 NotImplemented ──

    def embed(self, texts: Sequence[str]) -> EmbedResult:
        raise NotImplementedError(
            f"{type(self).__name__} 은 embed 를 지원하지 않는다 (role={self.role})"
        )

    def submit_batch(self, requests: Iterable[BatchRequest]) -> str:
        raise NotImplementedError(
            f"{type(self).__name__} 은 submit_batch 를 지원하지 않는다 (role={self.role})"
        )

    def fetch_batch(self, batch_id: str) -> BatchResults:
        raise NotImplementedError(
            f"{type(self).__name__} 은 fetch_batch 를 지원하지 않는다 (role={self.role})"
        )

    def complete(self, system: str, user: str, max_output_tokens: int | None = None) -> Completion:
        raise NotImplementedError(
            f"{type(self).__name__} 은 complete 를 지원하지 않는다 (role={self.role})"
        )

    # ── 공통 도우미 ────────────────────────────────────────────────────

    @property
    def max_output_tokens(self) -> int:
        return int(self.spec.get("max_output_tokens") or 0)

    @property
    def is_batch(self) -> bool:
        return self.mode == "batch"

    def __repr__(self) -> str:  # pragma: no cover - 디버깅용
        return f"<{type(self).__name__} role={self.role} model={self.model} mode={self.mode}>"
