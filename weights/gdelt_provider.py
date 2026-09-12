"""기본 provider — GDELT DOC 2.0 timelinesourcecountry.

"지금 금융·경제 뉴스에서 자주 언급되는 나라 순서" 를 재는 도구로 GDELT 는
목적에 정확히 맞고, 무료이며, 허용돼 있다. 스크래핑이 필요 없다.

응답의 시리즈 이름은 FIPS 코드가 아니라 **영문 국가명**이다.
config/countries.yaml 의 name_en 으로 되돌린다 — 여기서 조용히 실패하면
엉뚱한 나라의 가중치가 0이 되므로 매칭 실패는 경고로 남긴다.

요청 실패·부분 실패 시 이전 주 값을 재사용하고 경고를 남긴다.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config

from .base import AttentionProvider, VolumeSeries, register


@register
class GdeltProvider(AttentionProvider):
    name = "gdelt"

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        gd = config.sources().get("gdelt") or {}
        self.endpoint = gd.get("doc_api", "https://api.gdeltproject.org/api/v2/doc/doc")
        self.themes = list(gd.get("themes_for_weights") or [])
        self.timeout = int(gd.get("timeout_seconds", 45))
        self.retries = int(gd.get("retries", 3))
        #: 테스트가 주입하는 오프라인 응답 (tests/fixtures/*.json)
        self.fixture: Path | None = self.options.get("fixture")
        self.db = self.options.get("db")

    # ── 쿼리 ──────────────────────────────────────────────────────────

    def query_string(self) -> str:
        """경제 테마로 한정한 OR 쿼리.

        테마가 비어 있으면 전체 뉴스가 되어 가중치가 '보도량 많은 나라' 순서가
        되어버린다 — 그건 우리가 원하는 신호가 아니다.
        """
        if not self.themes:
            raise ValueError(
                "sources.yaml 의 gdelt.themes_for_weights 가 비어 있다. "
                "경제 테마로 한정하지 않으면 가중치가 의미를 잃는다."
            )
        return " OR ".join(f"theme:{t}" for t in self.themes)

    def params(self, lookback_days: int) -> dict[str, str]:
        return {
            "query": self.query_string(),
            "mode": "timelinesourcecountry",
            "timespan": f"{lookback_days}d",
            "format": "json",
        }

    # ── 인터페이스 ────────────────────────────────────────────────────

    def fetch(self, lookback_days: int) -> dict[str, float]:
        return self.fetch_series(lookback_days).totals()

    def fetch_series(self, lookback_days: int) -> VolumeSeries:
        raw = self._load(lookback_days)
        if raw is None:
            return self._fallback("GDELT 요청이 모두 실패했다")
        series = self.parse(raw)
        if series.is_empty():
            return self._fallback("GDELT 응답이 비어 있다")
        return series

    # ── 응답 로딩 ─────────────────────────────────────────────────────

    def _load(self, lookback_days: int) -> dict | None:
        if self.fixture is not None:
            return json.loads(Path(self.fixture).read_text(encoding="utf-8"))

        import requests  # noqa: PLC0415 — 오프라인 테스트에서는 import 되지 않는다

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.get(
                    self.endpoint, params=self.params(lookback_days), timeout=self.timeout
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 재시도 후 폴백
                last_error = exc
                print(
                    f"[weights] GDELT 요청 실패 ({attempt}/{self.retries}): {exc}",
                    file=sys.stderr,
                )
        print(f"[weights] GDELT 최종 실패: {last_error}", file=sys.stderr)
        return None

    # ── 파싱 ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(value: Any) -> str:
        s = str(value)
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return s

    def parse(self, raw: dict) -> VolumeSeries:
        """timelinesourcecountry 응답 → VolumeSeries.

        형태:
          {"timeline": [{"series": "United States",
                         "data": [{"date": "20260905T000000Z", "value": 3.2}, ...]}, ...]}
        """
        series = VolumeSeries()
        timeline = raw.get("timeline") or []
        unknown: list[str] = []
        all_dates: list[str] = []

        for entry in timeline:
            label = entry.get("series") or entry.get("name") or ""
            country = config.by_name_en(label)
            if country is None:
                unknown.append(label)
                continue
            points = entry.get("data") or []
            values = [float(p.get("value", 0) or 0) for p in points]
            dates = [self._parse_date(p.get("date")) for p in points]
            if not all_dates and dates:
                all_dates = dates
            # 같은 나라가 두 시리즈로 나뉘어 오면 더한다
            if country.fips in series.daily:
                prev = series.daily[country.fips]
                series.daily[country.fips] = [
                    a + b for a, b in zip(prev, values)
                ] + values[len(prev):]
            else:
                series.daily[country.fips] = values

        series.dates = all_dates
        if unknown:
            msg = f"이름을 FIPS로 못 되돌린 시리즈 {len(unknown)}개: {unknown[:5]}"
            series.warnings.append(msg)
            print(f"[weights] {msg}", file=sys.stderr)
            print(
                "[weights] config/countries.yaml 의 name_en 또는 NAME_ALIASES 에 추가해라",
                file=sys.stderr,
            )
        return series

    # ── 폴백 ──────────────────────────────────────────────────────────

    def _fallback(self, why: str) -> VolumeSeries:
        """이전 주 값을 재사용한다. 없으면 빈 시리즈 + 경고."""
        print(f"[weights] {why} — 이전 값을 재사용한다", file=sys.stderr)
        if self.db is None:
            return VolumeSeries(warnings=[f"{why}; 재사용할 이전 값도 없다"], stale=True)

        prev_date = self.db.latest_weight_date()
        if not prev_date:
            return VolumeSeries(warnings=[f"{why}; DB에 이전 가중치가 없다"], stale=True)

        rows = self.db.weights_for(prev_date)
        # attention 은 정규화된 값이므로 그대로 볼륨처럼 쓴다 (순위는 보존된다)
        daily = {r["fips"]: [float(r["attention"] or 0.0)] for r in rows}
        return VolumeSeries(
            daily=daily,
            dates=[prev_date],
            warnings=[f"{why}; {prev_date} 가중치를 재사용했다"],
            stale=True,
        )
