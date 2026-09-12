"""Google Gemini 어댑터.

저가안(gemini-2.5-flash-lite)으로 갈아탈 때 쓰인다.
models.yaml 의 provider 를 gemini 로 바꾸면 파이썬 코드 수정 없이 이 클래스가 선택된다.

벤더별 batch 차이를 여기서 흡수한다: Gemini 의 batch 는 JSONL 파일 업로드 기반이므로
제출 시 요청을 파일로 만들고, 수거 시 결과 파일을 파싱한다.
호출부는 submit_batch/fetch_batch 라는 같은 모양만 본다.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
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


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, role_spec: dict):
        super().__init__(role_spec)
        self._client = None

    def _sdk(self):
        if self._client is None:
            try:
                from google import genai  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "google-genai SDK가 없다. pip install google-genai 하거나 --dry-run 을 써라."
                ) from exc
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError(
                    "GEMINI_API_KEY 가 없다. --dry-run 을 쓰거나 키를 넣어라."
                )
            self._client = genai.Client(api_key=key)
        return self._client

    @staticmethod
    def _usage(meta, batch: bool) -> Usage:
        return Usage(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
            cached_input_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
            batch=batch,
        )

    def complete(
        self, system: str, user: str, max_output_tokens: int | None = None
    ) -> Completion:
        client = self._sdk()
        resp = client.models.generate_content(
            model=self.model,
            contents=user,
            config={
                "system_instruction": system,
                "max_output_tokens": max_output_tokens or self.max_output_tokens or 4096,
            },
        )
        return Completion(
            text=resp.text or "",
            model=self.model,
            usage=self._usage(getattr(resp, "usage_metadata", None), batch=False),
        )

    # ── Batch: JSONL 업로드 → job 생성 → 결과 파일 다운로드 ────────────

    def submit_batch(self, requests: Iterable[BatchRequest]) -> str:
        client = self._sdk()
        rows = []
        for req in requests:
            rows.append(
                {
                    "key": req.custom_id,
                    "request": {
                        "contents": [{"parts": [{"text": req.user}], "role": "user"}],
                        "system_instruction": {"parts": [{"text": req.system}]},
                        "generation_config": {
                            "max_output_tokens": (
                                req.max_output_tokens or self.max_output_tokens or 4096
                            )
                        },
                    },
                }
            )
        if not rows:
            raise ValueError("빈 배치는 제출하지 않는다")

        handle, name = tempfile.mkstemp(suffix=".jsonl", prefix="news-lens-batch-")
        os.close(handle)
        tmp = Path(name)
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )
        try:
            uploaded = client.files.upload(
                file=str(tmp), config={"mime_type": "application/jsonl"}
            )
            job = client.batches.create(model=self.model, src=uploaded.name)
        finally:
            tmp.unlink(missing_ok=True)
        return job.name

    def fetch_batch(self, batch_id: str) -> BatchResults:
        client = self._sdk()
        job = client.batches.get(name=batch_id)
        state = str(getattr(job.state, "name", job.state) or "")
        if state.endswith(("FAILED", "CANCELLED", "EXPIRED")):
            return BatchResults(
                batch_id=batch_id, status=BatchStatus.FAILED, model=self.model
            )
        if not state.endswith("SUCCEEDED"):
            return BatchResults(
                batch_id=batch_id, status=BatchStatus.PENDING, model=self.model
            )

        raw = client.files.download(file=job.dest.file_name).decode("utf-8")
        items: list[BatchItemResult] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = row.get("key", "")
            resp = row.get("response") or {}
            if row.get("error") or not resp:
                items.append(
                    BatchItemResult(
                        custom_id=key,
                        ok=False,
                        error=json.dumps(row.get("error") or {}, ensure_ascii=False),
                        usage=Usage(batch=True),
                    )
                )
                continue
            candidates = resp.get("candidates") or [{}]
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            meta = resp.get("usageMetadata") or {}
            items.append(
                BatchItemResult(
                    custom_id=key,
                    ok=True,
                    text=text,
                    usage=Usage(
                        input_tokens=meta.get("promptTokenCount", 0),
                        output_tokens=meta.get("candidatesTokenCount", 0),
                        batch=True,
                    ),
                )
            )
        return BatchResults(
            batch_id=batch_id, status=BatchStatus.ENDED, items=items, model=self.model
        )
