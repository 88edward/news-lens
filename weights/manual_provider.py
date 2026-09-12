"""수동 가중치 provider.

config/sources.yaml 의 weights.manual_scores 에 적은 국가별 점수를 그대로 돌려준다.
특정 나라를 강제로 올리거나 내릴 때 쓴다 — 운영 중 손으로 개입하는 유일한 통로.

    weights:
      providers:
        - { name: gdelt,  enabled: true,  blend: 0.8 }
        - { name: manual, enabled: true,  blend: 0.2 }
      manual_scores:
        KS: 500      # FIPS 코드로 적는다. ISO(KR) 가 아니다.
"""
from __future__ import annotations

import sys

import config

from .base import AttentionProvider, VolumeSeries, register


@register
class ManualProvider(AttentionProvider):
    name = "manual"

    def scores(self) -> dict[str, float]:
        cfg = (config.sources().get("weights") or {}).get("manual_scores") or {}
        known = config.known_fips()
        out: dict[str, float] = {}
        for code, value in cfg.items():
            key = str(code).upper()
            if key not in known:
                # ISO 코드를 실수로 적는 일이 잦다. 조용히 무시하지 않는다.
                hint = ""
                if config.by_iso(key) is not None:
                    hint = f" — ISO 코드로 보인다. FIPS 는 '{config.iso_to_fips(key)}' 다."
                print(
                    f"[weights] manual_scores 의 '{key}' 가 countries.yaml 에 없다{hint}",
                    file=sys.stderr,
                )
                continue
            out[key] = float(value)
        return out

    def fetch(self, lookback_days: int) -> dict[str, float]:
        return self.scores()

    def fetch_series(self, lookback_days: int) -> VolumeSeries:
        """수동 점수는 시간 축이 없다 — 평평한 시리즈로 준다.

        따라서 manual provider 는 attention 에만 기여하고 surge 에는 기여하지 않는다.
        의도된 동작이다: 손으로 '급등'을 조작할 수 있으면 신호가 아니게 된다.
        """
        totals = self.scores()
        n = max(1, lookback_days)
        return VolumeSeries(
            daily={fips: [v / n] * n for fips, v in totals.items()},
            warnings=[] if totals else ["manual_scores 가 비어 있다"],
        )
