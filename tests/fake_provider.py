"""테스트용 가짜 provider.

구현은 llm/fake_client.py 에 있다 — 오프라인 전체 실행(README)이 같은 코드를
쓰기 때문이다. 여기서는 테스트가 쓰는 이름만 다시 내보낸다.

실제 API 는 절대 호출하지 않는다.
"""
from __future__ import annotations

from llm.fake_client import FakeClient, deterministic_vector

__all__ = ["FakeClient", "deterministic_vector"]
