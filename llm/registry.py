"""역할 이름 → LLM 클라이언트.

    from llm.registry import get_client
    client = get_client("event_analysis")

역할을 추가할 때 이 파일을 고칠 일이 없다. models.yaml 에 역할을 적으면 끝이다.
provider 를 추가할 때도 이 파일을 고치지 않는다 — llm/ 에 LLMClient 하위 클래스를
가진 모듈을 하나 넣으면 자동으로 발견된다.
"""
from __future__ import annotations

import importlib
import pkgutil
import threading

import config

from .base import LLMClient

_providers: dict[str, type[LLMClient]] = {}
_clients: dict[str, LLMClient] = {}
_discovered = False
_lock = threading.RLock()


class ProviderError(RuntimeError):
    """provider 이름에 맞는 어댑터가 없을 때."""


def register(cls: type[LLMClient]) -> type[LLMClient]:
    """어댑터를 수동 등록한다. 테스트의 fake provider가 쓴다."""
    name = getattr(cls, "provider", "")
    if not name:
        raise ProviderError(f"{cls.__name__} 에 provider 이름이 없다")
    with _lock:
        _providers[name] = cls
    return cls


def _discover() -> None:
    """llm 패키지 안의 모듈을 훑어 LLMClient 하위 클래스를 자동 등록한다.

    벤더 SDK가 설치돼 있지 않아 import 가 실패하는 모듈은 조용히 건너뛴다 —
    dry-run 은 SDK 없이도 돌아야 하기 때문이다.
    """
    global _discovered
    with _lock:
        if _discovered:
            return
        package = importlib.import_module(__package__)
        for info in pkgutil.iter_modules(package.__path__):
            if info.name in ("base", "registry", "cost"):
                continue
            try:
                module = importlib.import_module(f"{__package__}.{info.name}")
            except Exception:  # noqa: BLE001 — SDK 미설치 모듈은 건너뛴다
                continue
            for attr in vars(module).values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, LLMClient)
                    and attr is not LLMClient
                    and getattr(attr, "provider", "")
                ):
                    _providers.setdefault(attr.provider, attr)
        _discovered = True


def provider_names() -> list[str]:
    _discover()
    return sorted(_providers)


def get_client(role_name: str, *, fresh: bool = False) -> LLMClient:
    """역할 이름으로 클라이언트를 얻는다.

    models.yaml 의 provider 한 줄만 바꾸면 다른 클래스가 돌아온다.
    호출부는 무엇이 돌아왔는지 알 필요가 없다.
    """
    _discover()
    spec = config.role(role_name)
    provider = spec["provider"]
    with _lock:
        if not fresh and role_name in _clients:
            cached = _clients[role_name]
            if cached.provider == provider and cached.model == spec["model"]:
                return cached
        cls = _providers.get(provider)
        if cls is None:
            raise ProviderError(
                f"provider '{provider}' 에 맞는 어댑터가 없다 (role={role_name}). "
                f"등록된 provider: {sorted(_providers)}. "
                "llm/ 에 LLMClient 하위 클래스를 가진 모듈을 추가해라."
            )
        client = cls(spec)
        _clients[role_name] = client
        return client


def reset() -> None:
    """캐시를 비운다. 테스트가 설정을 바꿔 끼울 때 쓴다."""
    global _discovered
    with _lock:
        _clients.clear()
        _providers.clear()
        _discovered = False
