"""여러 provider 를 합쳐 최종 국가 가중치를 만든다.

    attention = 국가별 점유율 (최상위 국가가 1.0 이 되도록 정규화)
    surge     = 어제 볼륨 ÷ 최근 lookback 일평균, 0~1 로 압축
    weight    = attention_coef × attention + surge_coef × surge

왜 surge 를 섞는가: attention 만 쓰면 미국·중국이 영구히 상위를 독식한다.
"평소엔 조용한데 오늘 갑자기 터진 나라" 가 올라와야 뉴스 사이트가 된다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

import config

from .base import AttentionProvider, VolumeSeries, get_provider

#: surge_raw 가 이 값 이상이면 surge=1.0 으로 본다.
#: 국가 간 min-max 정규화를 쓰지 않는 이유: 한 나라의 이상치가 나머지를 전부
#: 0 근처로 눌러버려 주 단위로 값이 널뛴다. 고정 상한이 훨씬 안정적이다.
DEFAULT_SURGE_CAP = 3.0


@dataclass
class CountryWeight:
    fips: str
    iso2: str
    attention: float = 0.0
    surge: float = 0.0
    surge_raw: float = 0.0
    weight: float = 0.0
    provider_breakdown: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "fips": self.fips,
            "iso2": self.iso2,
            "attention": round(self.attention, 6),
            "surge": round(self.surge, 6),
            "surge_raw": round(self.surge_raw, 6),
            "weight": round(self.weight, 6),
            "provider_breakdown": self.provider_breakdown,
        }


def formula() -> dict:
    return (config.sources().get("weights") or {}).get("formula") or {}


def enabled_providers() -> list[dict]:
    rows = (config.sources().get("weights") or {}).get("providers") or []
    out = [r for r in rows if r.get("enabled")]
    if not out:
        raise ValueError(
            "sources.yaml 의 weights.providers 에 enabled=true 인 provider 가 없다"
        )
    return out


# ── 정규화 ──────────────────────────────────────────────────────────────


def normalize_attention(totals: dict[str, float]) -> dict[str, float]:
    """점유율 → 0~1. 최상위 국가가 1.0.

    점유율(share)을 그대로 쓰면 상위 국가도 0.2 수준이라, 0~1 로 나오는 surge 와
    더할 때 계수(0.7/0.3)가 의도한 비중을 못 갖는다. 그래서 최댓값으로 나눈다.
    """
    total = sum(v for v in totals.values() if v > 0)
    if total <= 0:
        return {k: 0.0 for k in totals}
    shares = {k: max(0.0, v) / total for k, v in totals.items()}
    top = max(shares.values()) or 1.0
    return {k: v / top for k, v in shares.items()}


def compute_surge(series: VolumeSeries, cap: float = DEFAULT_SURGE_CAP) -> tuple[dict, dict]:
    """(정규화된 surge, 원시 배율) 을 돌려준다.

    원시 배율은 '어제가 평소의 몇 배인가' 라서 사람이 읽기 좋고, 글로브의 링
    표시 기준(>1.8)에도 그대로 쓰인다. 그래서 둘 다 저장한다.
    """
    yesterday = series.yesterday()
    mean = series.mean()
    raw: dict[str, float] = {}
    norm: dict[str, float] = {}
    for fips in series.countries():
        avg = mean.get(fips, 0.0)
        y = yesterday.get(fips, 0.0)
        if avg <= 0:
            raw[fips] = 0.0
            norm[fips] = 0.0
            continue
        ratio = y / avg
        raw[fips] = ratio
        # 1.0(평소와 같음) → 0, cap 이상 → 1
        norm[fips] = min(1.0, max(0.0, (ratio - 1.0) / (cap - 1.0))) if cap > 1 else 0.0
    return norm, raw


# ── 혼합 ────────────────────────────────────────────────────────────────


def blend(
    providers: list[AttentionProvider] | None = None,
    *,
    lookback_days: int | None = None,
    surge_cap: float = DEFAULT_SURGE_CAP,
    options: dict | None = None,
) -> list[CountryWeight]:
    """enabled provider 들을 blend 값으로 가중 평균한다."""
    fml = formula()
    lookback = lookback_days or int(fml.get("lookback_days", 7))
    a_coef = float(fml.get("attention_coef", 0.7))
    s_coef = float(fml.get("surge_coef", 0.3))

    specs = enabled_providers()
    if providers is None:
        providers = [get_provider(s["name"], options) for s in specs]
    blends = {s["name"]: float(s.get("blend", 1.0)) for s in specs}

    per_provider: dict[str, dict] = {}
    total_blend = 0.0
    for provider in providers:
        share = blends.get(provider.name, 1.0)
        if share <= 0:
            continue
        series = provider.fetch_series(lookback)
        for w in series.warnings:
            print(f"[weights] {provider.name}: {w}", file=sys.stderr)
        attention = normalize_attention(series.totals())
        surge, surge_raw = compute_surge(series, cap=surge_cap)
        per_provider[provider.name] = {
            "attention": attention,
            "surge": surge,
            "surge_raw": surge_raw,
            "blend": share,
            "stale": series.stale,
        }
        total_blend += share

    if total_blend <= 0:
        raise ValueError("blend 합이 0이다 — 가중치를 계산할 수 없다")

    all_fips: set[str] = set()
    for data in per_provider.values():
        all_fips |= set(data["attention"])

    results: list[CountryWeight] = []
    for fips in sorted(all_fips):
        country = config.by_fips(fips)
        if country is None:
            continue  # countries.yaml 에 없는 나라는 수집 대상이 아니다
        attention = 0.0
        surge = 0.0
        surge_raw = 0.0
        breakdown: dict = {}
        for pname, data in per_provider.items():
            share = data["blend"] / total_blend
            a = data["attention"].get(fips, 0.0)
            s = data["surge"].get(fips, 0.0)
            attention += a * share
            surge += s * share
            surge_raw = max(surge_raw, data["surge_raw"].get(fips, 0.0))
            breakdown[pname] = {
                "attention": round(a, 6),
                "surge": round(s, 6),
                "blend": round(share, 6),
                "stale": data["stale"],
            }
        results.append(
            CountryWeight(
                fips=fips,
                iso2=country.iso2,
                attention=attention,
                surge=surge,
                surge_raw=surge_raw,
                weight=a_coef * attention + s_coef * surge,
                provider_breakdown=breakdown,
            )
        )

    # 최종 weight 를 0~1 로 맞춘다 — 글로브의 마커 밝기가 이 값을 그대로 쓴다.
    top = max((r.weight for r in results), default=0.0)
    if top > 0:
        for r in results:
            r.weight /= top

    results.sort(key=lambda r: r.weight, reverse=True)
    return results
