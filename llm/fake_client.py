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
import os
import re
import sys
from pathlib import Path
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

#: 제출한 배치를 남겨 두는 곳.
#: 클래스 변수만 쓰면 프로세스가 끝나는 순간 사라져서, step5 → step6 처럼
#: 제출과 수거가 다른 실행인 구조를 오프라인에서 재현할 수 없다.
#: 실제 Batch API 와 같은 모양으로 돌아가야 README 의 "fixtures 로 전체 돌리기"가
#: 거짓말이 아니게 된다.
def _store_dir() -> Path:
    path = Path(os.environ.get("NEWS_LENS_FAKE_DIR", "data/fake-batches"))
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        batch_id = f"fake_batch_{self.role or 'x'}_{type(self).submit_count:03d}"
        type(self).batch_store[batch_id] = reqs
        self._persist(batch_id, reqs)
        return batch_id

    def _persist(self, batch_id: str, reqs: list[BatchRequest]) -> None:
        rows = [
            {
                "custom_id": r.custom_id,
                "system": r.system,
                "user": r.user,
                "max_output_tokens": r.max_output_tokens,
            }
            for r in reqs
        ]
        (_store_dir() / f"{batch_id}.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )

    def _load(self, batch_id: str) -> list[BatchRequest]:
        path = _store_dir() / f"{batch_id}.json"
        if not path.exists():
            return []
        return [
            BatchRequest(
                custom_id=row["custom_id"],
                system=row["system"],
                user=row["user"],
                max_output_tokens=row.get("max_output_tokens"),
            )
            for row in json.loads(path.read_text(encoding="utf-8"))
        ]

    def fetch_batch(self, batch_id: str) -> BatchResults:
        if batch_id in type(self).pending_batches:
            return BatchResults(
                batch_id=batch_id, status=BatchStatus.PENDING, model=self.model
            )
        # 같은 프로세스면 메모리에서, 다른 실행이면 디스크에서 읽는다.
        reqs = type(self).batch_store.get(batch_id) or self._load(batch_id)
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
        return json.dumps(self._synthesize(req), ensure_ascii=False)

    def _synthesize(self, req: BatchRequest) -> dict:
        """역할의 스키마를 통과하는 그럴듯한 응답을 만든다.

        의미 없는 에코를 돌려주면 step6 이 전부 검증 실패로 처리해서
        step7·step8 을 오프라인에서 볼 수 없다.
        """
        try:
            import config  # noqa: PLC0415 — 순환 import 를 피하려고 지연시킨다

            schema = config.schema_for(self.role)
        except Exception:  # noqa: BLE001
            return {"echo": req.custom_id}
        # 브리핑은 실제로 존재하는 사건 id 만 써야 한다.
        # 없는 id 를 지어내면 step7 이 (정당하게) 거부한다.
        event_ids = re.findall(r"evt_\d{8}_\d{4}", req.user)[:2]
        return _sample(schema, seed=req.custom_id, event_ids=event_ids)

    @classmethod
    def clear_disk(cls) -> None:
        for path in _store_dir().glob("fake_batch_*.json"):
            path.unlink(missing_ok=True)

    @classmethod
    def reset(cls) -> None:
        cls.pending_batches = set()
        cls.batch_store = {}
        cls.responder = None
        cls.submit_count = 0
        _warned.clear()


# ── 스키마로부터 표본 만들기 ────────────────────────────────────────────


def _sample(schema: dict, *, seed: str = "", event_ids: list[str] | None = None):
    """JSON Schema 를 보고 검증을 통과하는 최소 표본을 만든다.

    완전한 구현이 아니다 — prompts/_shared/ 의 두 스키마가 쓰는 만큼만 다룬다.
    """
    kind = schema.get("type")

    if kind == "object":
        out = {}
        props = schema.get("properties") or {}
        for key in schema.get("required") or list(props):
            spec = props.get(key) or {"type": "string"}
            if key == "관련사건":
                out[key] = list(event_ids or [])
            else:
                out[key] = _sample(spec, seed=f"{seed}/{key}", event_ids=event_ids)
        return out

    if kind == "array":
        item = schema.get("items") or {"type": "string"}
        n = max(1, int(schema.get("minItems", 1)))
        return [
            _sample(item, seed=f"{seed}[{i}]", event_ids=event_ids) for i in range(n)
        ]

    if kind == "number" or kind == "integer":
        low = schema.get("minimum", 0)
        high = schema.get("maximum", low + 1)
        # seed 로 값을 흩뿌려 두면 글로브 마커 색이 전부 같아지지 않는다.
        spread = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:4], 16) / 0xFFFF
        value = low + (high - low) * spread
        return round(value, 2) if kind == "number" else int(value)

    if kind == "boolean":
        return True

    # string
    if schema.get("enum"):
        idx = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:4], 16)
        options = schema["enum"]
        return options[idx % len(options)]
    if schema.get("pattern") and event_ids:
        return event_ids[0]

    text = f"오프라인 표본 응답 ({seed})."
    minimum = int(schema.get("minLength", 0))
    while len(text) < minimum:
        text += " 이 문장은 스키마의 최소 길이를 채우기 위한 자리표시자다."
    maximum = schema.get("maxLength")
    return text[:maximum] if maximum else text


def prune_batches(*, days: int = 2, dry_run: bool = False) -> tuple[int, int]:
    """오프라인 배치 파일 정리. (파일 수, 바이트)

    오프라인으로 계속 돌리면 step5 가 제출할 때마다 파일이 하나씩 쌓인다.
    운영(진짜 API)에서는 생기지 않지만, 로컬에서 반복 실행하면 무한히 는다.
    """
    import time

    cutoff = time.time() - days * 86400
    removed = 0
    freed = 0
    for path in _store_dir().glob("fake_batch_*.json"):
        if path.stat().st_mtime >= cutoff:
            continue
        freed += path.stat().st_size
        if not dry_run:
            path.unlink(missing_ok=True)
        removed += 1
    return (removed, freed)
