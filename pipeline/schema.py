"""LLM 응답 파싱과 스키마 검증.

검증에 실패한 응답은 **버리지 않는다.** raw 그대로 남기고 실패 카운트를 올린다.
그게 프롬프트를 고칠 유일한 근거다.

jsonschema 패키지가 없어도 최소한의 구조 검증은 되도록 폴백을 둔다 —
dry-run 이 의존성 없이 돌아야 하기 때문이다.
"""
from __future__ import annotations

import json
import re

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(text: str) -> dict | None:
    """모델 응답에서 JSON 객체를 꺼낸다.

    코드 펜스를 붙이지 말라고 지시해도 가끔 붙는다. 앞뒤 설명 문장도 마찬가지다.
    프롬프트로 막되, 파서도 관대하게 만든다 — 이걸로 버려지는 응답이 아깝다.
    """
    if not text:
        return None
    cleaned = FENCE.sub("", text).strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    # 가장 바깥 중괄호 쌍을 찾아 다시 시도한다
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start : end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def validate(payload: dict, schema: dict) -> list[str]:
    """스키마 위반 목록. 빈 리스트면 통과."""
    try:
        import jsonschema  # noqa: PLC0415

        validator = jsonschema.Draft202012Validator(schema)
        return [
            f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
            for e in sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        ]
    except ImportError:
        return _minimal_validate(payload, schema)


def _minimal_validate(payload: dict, schema: dict) -> list[str]:
    """jsonschema 가 없을 때의 최소 검증: 필수 키와 최상위 타입만 본다."""
    errors = []
    for key in schema.get("required", []):
        if key not in payload:
            errors.append(f"{key}: 필수 항목이 없다")
    props = schema.get("properties") or {}
    for key, value in payload.items():
        spec = props.get(key)
        if not spec:
            continue
        expected = spec.get("type")
        checks = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "array": list,
            "object": dict,
            "boolean": bool,
        }
        if expected in checks and not isinstance(value, checks[expected]):
            errors.append(f"{key}: {expected} 여야 하는데 {type(value).__name__} 이다")
    return errors


def parse_and_validate(text: str, schema: dict) -> tuple[dict | None, list[str]]:
    payload = extract_json(text)
    if payload is None:
        return None, ["응답에서 JSON 객체를 찾지 못했다"]
    return payload, validate(payload, schema)
