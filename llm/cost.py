"""usage × 단가 → 비용. 그리고 일일 상한 강제.

규칙: 경고만 찍고 진행하는 것은 통제가 아니다.
config/pipeline.yaml 의 cost.max_daily_cost_usd 를 넘으면 예외를 던진다.

    from llm import cost
    cost.preflight("event_analysis", in_tokens=..., out_tokens=..., n=180)  # 호출 전
    cost.record("event_analysis", usage, ledger=ledger)                      # 호출 후
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date

import config

from .base import Usage

#: 캐시 읽기는 입력 단가의 10%로 계산한다 (Anthropic·OpenAI 공통 관행).
CACHE_READ_MULTIPLIER = 0.1


class CostLimitExceeded(RuntimeError):
    """일일 비용 상한 초과. 파이프라인은 여기서 멈춘다."""


def _cost_cfg() -> dict:
    return config.pipeline().get("cost") or {}


def max_daily_cost_usd() -> float:
    value = _cost_cfg().get("max_daily_cost_usd")
    if value is None:
        raise config.ConfigError(
            "pipeline.yaml 에 cost.max_daily_cost_usd 가 없다. 상한 없는 실행은 금지다."
        )
    return float(value)


def price_of(role_name: str, usage: Usage) -> float:
    """한 번의 호출 비용(USD). batch면 할인율을 적용한다."""
    spec = config.role(role_name)
    price_in = float(spec.get("price_per_mtok_in") or 0.0)
    price_out = float(spec.get("price_per_mtok_out") or 0.0)

    discount = 1.0
    if usage.batch:
        discount = float(spec.get("batch_discount") or 1.0)

    billable_in = usage.input_tokens + usage.cached_input_tokens * CACHE_READ_MULTIPLIER
    total = (billable_in * price_in + usage.output_tokens * price_out) / 1_000_000.0
    return total * discount


def estimate(
    role_name: str,
    *,
    in_tokens: int,
    out_tokens: int,
    n: int = 1,
    batch: bool | None = None,
) -> float:
    """호출 전 예상 비용. batch 여부를 안 주면 models.yaml 의 mode 를 따른다."""
    spec = config.role(role_name)
    is_batch = spec["mode"] == "batch" if batch is None else batch
    per_call = price_of(
        role_name,
        Usage(input_tokens=in_tokens, output_tokens=out_tokens, batch=is_batch),
    )
    return per_call * n


@dataclass
class Ledger:
    """하루치 누적 비용. store/db.py 의 runs 테이블과 짝을 이룬다.

    DB 없이도 동작하도록 메모리 누적을 기본으로 두고, db 를 주면 runs 에 기록한다.
    """

    day: date = field(default_factory=date.today)
    spent_usd: float = 0.0
    by_role: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    db: object | None = None

    # ── 조회 ──────────────────────────────────────────────────────────

    @property
    def limit(self) -> float:
        return max_daily_cost_usd()

    @property
    def remaining(self) -> float:
        return self.limit - self.spent_usd

    def load_from_db(self) -> None:
        """오늘 이미 쓴 금액을 runs 테이블에서 읽어 온다.

        step5와 step7은 서로 다른 프로세스다. DB를 안 읽으면 상한이 실행마다
        초기화되어 사실상 상한이 없는 것과 같아진다.
        """
        if self.db is None:
            return
        self.spent_usd = float(self.db.spent_today(self.day.isoformat()) or 0.0)

    # ── 기록 ──────────────────────────────────────────────────────────

    def record(self, role_name: str, usage: Usage, *, note: str = "") -> float:
        """실제 usage 를 누적한다. 누적 후 상한을 넘었으면 예외를 던진다."""
        amount = price_of(role_name, usage)
        self.spent_usd += amount
        self.by_role[role_name] = self.by_role.get(role_name, 0.0) + amount
        self.calls[role_name] = self.calls.get(role_name, 0) + 1
        if self.db is not None:
            self.db.record_run(
                day=self.day.isoformat(),
                role=role_name,
                model=config.role(role_name)["model"],
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=amount,
                note=note,
            )
        self._check(context=f"{role_name} 호출 후")
        return amount

    def preflight(
        self,
        role_name: str,
        *,
        in_tokens: int,
        out_tokens: int,
        n: int = 1,
        batch: bool | None = None,
    ) -> float:
        """호출 전 예상 비용을 stderr 에 찍고, 상한을 넘길 것 같으면 중단한다."""
        projected = estimate(
            role_name, in_tokens=in_tokens, out_tokens=out_tokens, n=n, batch=batch
        )
        spec = config.role(role_name)
        print(
            f"[cost] {role_name} · {spec['model']} · {spec['mode']} · {n}건 "
            f"· 입력 {in_tokens * n:,} tok · 출력 {out_tokens * n:,} tok "
            f"· 예상 ${projected:.4f} "
            f"· 오늘 누적 ${self.spent_usd:.4f} / 상한 ${self.limit:.2f}",
            file=sys.stderr,
        )
        if self.spent_usd + projected > self.limit:
            raise CostLimitExceeded(
                f"예상 비용 ${projected:.4f} 를 더하면 "
                f"${self.spent_usd + projected:.4f} 로 일일 상한 ${self.limit:.2f} 를 넘는다. "
                f"중단한다. (role={role_name}, {n}건)\n"
                "수집량을 줄이거나 config/pipeline.yaml 의 max_daily_cost_usd 를 올려라."
            )
        self._warn_if_close(projected)
        return projected

    # ── 내부 ──────────────────────────────────────────────────────────

    def _warn_if_close(self, projected: float = 0.0) -> None:
        frac = float(_cost_cfg().get("warn_at_fraction") or 0.8)
        if self.limit <= 0:
            return
        if (self.spent_usd + projected) / self.limit >= frac:
            print(
                f"[cost] 경고: 오늘 예산의 "
                f"{(self.spent_usd + projected) / self.limit:.0%} 를 쓴다",
                file=sys.stderr,
            )

    def _check(self, context: str = "") -> None:
        if self.spent_usd > self.limit:
            raise CostLimitExceeded(
                f"일일 누적 ${self.spent_usd:.4f} 가 상한 ${self.limit:.2f} 를 넘었다"
                + (f" ({context})" if context else "")
            )

    def report(self) -> str:
        lines = [
            f"오늘({self.day}) 비용: ${self.spent_usd:.4f} / 상한 ${self.limit:.2f}"
        ]
        for role_name in sorted(self.by_role):
            lines.append(
                f"  {role_name:<18} ${self.by_role[role_name]:.4f} "
                f"({self.calls.get(role_name, 0)}회)"
            )
        return "\n".join(lines)


_default_ledger: Ledger | None = None


def ledger(db: object | None = None, day: date | str | None = None) -> Ledger:
    """프로세스 하나가 공유하는 기본 원장.

    day 는 **파이프라인의 기준 날짜**(--date)를 넘겨라. date.today() 로 두면
    runs 테이블이 두 날짜로 갈라진다 — step5/step7 은 파이프라인 날짜로,
    원장은 실행 시각의 날짜로 쓰기 때문이다. 자정을 넘겨 도는 실행이나
    과거 날짜 재처리에서 일일 상한이 엉뚱한 날에 걸린다.
    """
    global _default_ledger
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if _default_ledger is None or day is not None or db is not None:
        _default_ledger = Ledger(day=day or date.today(), db=db)
        _default_ledger.load_from_db()
    return _default_ledger


def reset() -> None:
    global _default_ledger
    _default_ledger = None
