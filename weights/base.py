"""국가 관심도 신호 provider 인터페이스.

pipeline/ 은 이 인터페이스만 안다. 어떤 데이터 소스에서 오는지는 몰라야 한다.
나중에 정식 라이선스를 얻은 금융 데이터 소스가 생기면 파일 하나만 추가한다.

provider 는 **원시 볼륨만** 돌려준다. 정규화·혼합은 blend.py 가 한다.
그래야 provider 를 섞을 때 스케일이 다른 신호를 공정하게 합칠 수 있다.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class VolumeSeries:
    """국가별 일자별 원시 볼륨.

    daily[fips] = [가장 오래된 날, ..., 어제] 순서의 값 목록.
    surge 계산이 '어제 ÷ 최근 평균' 이므로 순서가 의미를 갖는다.
    """

    daily: dict[str, list[float]] = field(default_factory=dict)
    #: 이 시리즈가 어느 날짜들을 담고 있는지 (YYYY-MM-DD, daily 와 같은 순서)
    dates: list[str] = field(default_factory=list)
    #: 부분 실패 등으로 신뢰도가 떨어질 때 남기는 메모
    warnings: list[str] = field(default_factory=list)
    #: 이전 주 값을 재사용했는지
    stale: bool = False

    def totals(self) -> dict[str, float]:
        """lookback 기간 전체 합. attention 의 분자가 된다."""
        return {fips: float(sum(vals)) for fips, vals in self.daily.items()}

    def yesterday(self) -> dict[str, float]:
        return {
            fips: float(vals[-1]) if vals else 0.0 for fips, vals in self.daily.items()
        }

    def mean(self) -> dict[str, float]:
        out = {}
        for fips, vals in self.daily.items():
            out[fips] = float(sum(vals)) / len(vals) if vals else 0.0
        return out

    def countries(self) -> set[str]:
        return set(self.daily)

    def is_empty(self) -> bool:
        return not any(sum(v) for v in self.daily.values())


class AttentionProvider(abc.ABC):
    """국가 관심도 신호 하나.

    이름은 config/sources.yaml 의 weights.providers[].name 과 맞춘다.
    """

    name: str = ""

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    @abc.abstractmethod
    def fetch(self, lookback_days: int) -> dict[str, float]:
        """FIPS 코드 → 기간 전체 원시 볼륨.

        인터페이스의 최소 계약이다. surge 까지 계산하려면 fetch_series 를 구현해라.
        """

    def fetch_series(self, lookback_days: int) -> VolumeSeries:
        """일자별 시리즈. 기본 구현은 fetch 결과를 균등 분배한 평평한 시리즈다.

        평평한 시리즈에서는 surge 가 항상 1.0 이 된다 — 급등을 못 잡는다는 뜻이므로,
        급등 신호를 주고 싶은 provider 는 이 메서드를 직접 구현해야 한다.
        """
        totals = self.fetch(lookback_days)
        n = max(1, lookback_days)
        return VolumeSeries(
            daily={fips: [v / n] * n for fips, v in totals.items()},
            dates=[],
            warnings=[f"{self.name}: 일자별 시리즈가 없어 surge 를 계산할 수 없다"],
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name}>"


class ProviderError(RuntimeError):
    """provider 이름이 설정에 없거나 구현이 없을 때."""


_registry: dict[str, type[AttentionProvider]] = {}


def register(cls: type[AttentionProvider]) -> type[AttentionProvider]:
    if not getattr(cls, "name", ""):
        raise ProviderError(f"{cls.__name__} 에 name 이 없다")
    _registry[cls.name] = cls
    return cls


def get_provider(name: str, options: dict | None = None) -> AttentionProvider:
    _discover()
    if name not in _registry:
        raise ProviderError(
            f"관심도 provider '{name}' 이 없다. 있는 것: {sorted(_registry)}"
        )
    return _registry[name](options)


def provider_names() -> list[str]:
    _discover()
    return sorted(_registry)


_discovered = False


def _discover() -> None:
    """weights/ 안의 모듈을 훑어 provider 를 자동 등록한다."""
    global _discovered
    if _discovered:
        return
    import importlib
    import pkgutil

    package = importlib.import_module(__package__)
    for info in pkgutil.iter_modules(package.__path__):
        if info.name in ("base", "blend"):
            continue
        try:
            module = importlib.import_module(f"{__package__}.{info.name}")
        except Exception:  # noqa: BLE001
            continue
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, AttentionProvider)
                and attr is not AttentionProvider
                and getattr(attr, "name", "")
            ):
                _registry.setdefault(attr.name, attr)
    _discovered = True


def reset() -> None:
    global _discovered
    _registry.clear()
    _discovered = False
