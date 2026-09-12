"""설정 로더.

YAML을 읽어 캐시하고, FIPS↔ISO 변환 헬퍼를 제공한다.
파이프라인 코드는 여기를 통해서만 설정을 읽는다.

    from config import load, countries, fips_to_iso
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONFIG_DIR.parent

_cache: dict[str, Any] = {}
_lock = threading.Lock()


class ConfigError(RuntimeError):
    """설정 파일이 없거나 필수 키가 빠졌을 때."""


def load(name: str) -> Any:
    """config/<name>.yaml 을 읽어 돌려준다. 결과는 캐시된다.

    NEWS_LENS_CONFIG_DIR 로 디렉토리를 바꿀 수 있다 (테스트가 쓴다).
    """
    base = Path(os.environ.get("NEWS_LENS_CONFIG_DIR", CONFIG_DIR))
    key = f"{base}::{name}"
    with _lock:
        if key in _cache:
            return _cache[key]
        path = base / f"{name}.yaml"
        if not path.exists():
            raise ConfigError(f"설정 파일이 없다: {path}")
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        _cache[key] = data
        return data


def reset_cache() -> None:
    """테스트에서 설정 디렉토리를 바꿔 끼울 때 호출한다."""
    with _lock:
        _cache.clear()


def models() -> dict:
    return load("models")


def sources() -> dict:
    return load("sources")


def pipeline() -> dict:
    return load("pipeline")


def role(name: str) -> dict:
    """models.yaml 의 역할 하나를 defaults와 합쳐 돌려준다."""
    cfg = models()
    roles = cfg.get("roles") or {}
    if name not in roles:
        raise ConfigError(
            f"models.yaml 에 역할 '{name}' 이 없다. 있는 역할: {sorted(roles)}"
        )
    merged = dict(cfg.get("defaults") or {})
    merged.update(roles[name])
    merged["role"] = name
    for required in ("provider", "model", "mode"):
        if required not in merged:
            raise ConfigError(f"역할 '{name}' 에 '{required}' 가 없다")
    if merged["mode"] not in ("batch", "sync"):
        raise ConfigError(f"역할 '{name}' 의 mode는 batch 또는 sync 여야 한다")
    return merged


def role_names() -> list[str]:
    return sorted((models().get("roles") or {}).keys())


def prompt_text(role_name: str) -> str:
    """역할에 딸린 지시문 파일을 읽는다. 문구는 코드에 두지 않는다."""
    spec = role(role_name)
    rel = spec.get("prompt")
    if not rel:
        raise ConfigError(f"역할 '{role_name}' 에 prompt 경로가 없다")
    path = REPO_ROOT / rel
    if not path.exists():
        raise ConfigError(f"지시문 파일이 없다: {path}")
    return path.read_text(encoding="utf-8")


def schema_for(role_name: str) -> dict:
    """역할에 딸린 JSON 스키마를 읽는다."""
    import json

    spec = role(role_name)
    rel = spec.get("schema")
    if not rel:
        raise ConfigError(f"역할 '{role_name}' 에 schema 경로가 없다")
    path = REPO_ROOT / rel
    if not path.exists():
        raise ConfigError(f"스키마 파일이 없다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ── 국가 코드 ────────────────────────────────────────────────────────────
# GDELT는 FIPS 10-4, 나머지 세상은 ISO 3166-1 alpha-2를 쓴다.
# 변환은 반드시 여기를 경유한다. 코드 안에서 임시 변환을 만들지 마라.


@dataclass(frozen=True)
class Country:
    iso2: str
    fips: str
    name_ko: str
    lat: float
    lng: float
    name_en: str = ""


def countries() -> list[Country]:
    key = "__countries__"
    with _lock:
        if key in _cache:
            return _cache[key]
    rows = load("countries")
    out = []
    for row in rows:
        missing = {"iso2", "fips", "name_ko", "lat", "lng"} - set(row)
        if missing:
            raise ConfigError(f"countries.yaml 항목에 {missing} 가 없다: {row}")
        for field in ("iso2", "fips"):
            if not isinstance(row[field], str):
                # YAML 1.1 은 따옴표 없는 NO/ON/Y 를 boolean 으로 읽는다.
                # 노르웨이(NO)가 False 가 되는 고전적인 사고다.
                raise ConfigError(
                    f"countries.yaml 의 {field} 값 {row[field]!r} 이 문자열이 아니다. "
                    "국가 코드는 반드시 따옴표로 감싸라 (예: iso2: \"NO\")."
                )
        out.append(
            Country(
                iso2=row["iso2"].upper(),
                fips=row["fips"].upper(),
                name_ko=row["name_ko"],
                name_en=row.get("name_en", ""),
                lat=float(row["lat"]),
                lng=float(row["lng"]),
            )
        )
    by_fips = {c.fips for c in out}
    if len(by_fips) != len(out):
        raise ConfigError("countries.yaml 에 FIPS 코드가 중복이다")
    with _lock:
        _cache[key] = out
    return out


def _fips_index() -> dict[str, Country]:
    return {c.fips: c for c in countries()}


def _iso_index() -> dict[str, Country]:
    return {c.iso2: c for c in countries()}


def by_fips(code: str) -> Country | None:
    return _fips_index().get((code or "").upper())


def by_iso(code: str) -> Country | None:
    return _iso_index().get((code or "").upper())


def fips_to_iso(code: str) -> str:
    c = by_fips(code)
    if c is None:
        raise ConfigError(
            f"FIPS 코드 '{code}' 가 countries.yaml 에 없다. "
            "GDELT는 FIPS를 쓴다 — ISO 코드를 넣지 않았는지 확인해라."
        )
    return c.iso2


def iso_to_fips(code: str) -> str:
    c = by_iso(code)
    if c is None:
        raise ConfigError(f"ISO 코드 '{code}' 가 countries.yaml 에 없다")
    return c.fips


def known_fips() -> set[str]:
    return set(_fips_index())


def known_iso() -> set[str]:
    return set(_iso_index())


def _name_index() -> dict[str, Country]:
    """영문 국가명 → Country.

    GDELT timelinesourcecountry 는 FIPS 코드가 아니라 영문 국가명을 시리즈
    이름으로 돌려준다. 그 이름을 다시 FIPS 로 되돌리기 위한 인덱스다.
    """
    idx: dict[str, Country] = {}
    for c in countries():
        if c.name_en:
            idx[_normalize_name(c.name_en)] = c
    for alias, iso in NAME_ALIASES.items():
        target = by_iso(iso)
        if target is not None:
            idx.setdefault(_normalize_name(alias), target)
    return idx


def _normalize_name(name: str) -> str:
    return " ".join((name or "").lower().replace(".", "").split())


#: GDELT 가 쓰는 다른 표기들. 여기 없는 이름은 조용히 버려지지 않고 경고가 찍힌다.
NAME_ALIASES: dict[str, str] = {
    "united states of america": "US",
    "usa": "US",
    "korea south": "KR",
    "republic of korea": "KR",
    "south korea": "KR",
    "korea north": "KP",
    "russian federation": "RU",
    "united kingdom of great britain": "GB",
    "britain": "GB",
    "uk": "GB",
    "czechia": "CZ",
    "turkiye": "TR",
    "viet nam": "VN",
    "united arab emirates": "AE",
    "hong kong sar": "HK",
    "taiwan province of china": "TW",
    "iran islamic republic of": "IR",
}


def by_name_en(name: str) -> Country | None:
    return _name_index().get(_normalize_name(name))
