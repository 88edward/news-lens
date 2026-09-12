"""공통 픽스처.

핵심: 테스트는 실제 API 키를 절대 쓰지 않는다. 환경변수를 지워 두어
실수로 네트워크를 때리면 어댑터가 즉시 예외를 던지게 한다.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clean_registry():
    import config
    from llm import cost, registry

    config.reset_cache()
    registry.reset()
    cost.reset()
    yield
    config.reset_cache()
    registry.reset()
    cost.reset()


@pytest.fixture
def fake_client(tmp_path, monkeypatch):
    """fake provider 를 등록하고 클래스 상태를 초기화한다.

    제출한 배치를 디스크에 남기므로(프로세스가 달라도 수거되도록) 저장 위치를
    tmp 로 돌려 레포를 더럽히지 않게 한다.
    """
    from llm import registry

    from .fake_provider import FakeClient

    monkeypatch.setenv("NEWS_LENS_FAKE_DIR", str(tmp_path / "fake-batches"))
    FakeClient.reset()
    registry.register(FakeClient)
    yield FakeClient
    FakeClient.reset()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """진짜 config/ 를 tmp 로 복사해 마음껏 수정할 수 있게 한다.

    models.yaml 의 provider 한 줄을 바꿔도 코드가 안 바뀌는지 검증하는 데 쓴다.
    """
    import config as config_mod

    dst = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", dst)
    monkeypatch.setenv("NEWS_LENS_CONFIG_DIR", str(dst))
    config_mod.reset_cache()

    class Handle:
        path = dst

        @staticmethod
        def patch(name: str, mutate):
            file = dst / f"{name}.yaml"
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
            mutate(data)
            file.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            config_mod.reset_cache()

    yield Handle
    config_mod.reset_cache()
