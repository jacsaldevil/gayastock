"""Operational market-regime override for steady high-volatility recoveries.

The base strategy intentionally requires a very strong V-shaped rebound. This
module adds a narrower, smaller-scale path for sessions where the market has
recovered above MA5 and 5-day momentum is positive, but the same-day gain is
not large enough to satisfy the original volatile-rebound trigger.
"""
from __future__ import annotations

from typing import Any

from strategy.rules import evaluate_market_regime as _base_evaluate_market_regime


def _metric(result: dict[str, Any], key: str) -> float:
    return float(result.get(key, 0) or 0)


def _steady_high_vol_recovery(result: dict[str, Any]) -> bool:
    """Return True for a controlled high-volatility recovery, not a chase."""
    close = _metric(result, "close")
    ma5 = _metric(result, "ma5")
    ma60 = _metric(result, "ma60")
    change = _metric(result, "change_rate")
    return_5d = _metric(result, "return_5d_pct")
    ma20_slope = _metric(result, "ma20_slope_pct")
    drawdown = _metric(result, "drawdown_20d_pct")
    volatility = _metric(result, "realized_vol_20d_pct")

    return (
        result.get("status") in ("risk_off", "risk_off_selective")
        and close < ma60
        and ma5 > 0
        and close >= ma5 * 1.02
        and 0.3 <= change <= 8.0
        and return_5d >= 1.0
        and ma20_slope > -8.0
        and drawdown >= -25.0
        and 5.5 <= volatility <= 8.0
    )


def _high_vol_failed_conditions(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Explain which confirmation prevented the high-volatility recovery path."""
    close = _metric(result, "close")
    ma5 = _metric(result, "ma5")
    change = _metric(result, "change_rate")
    return_5d = _metric(result, "return_5d_pct")
    ma20_slope = _metric(result, "ma20_slope_pct")
    drawdown = _metric(result, "drawdown_20d_pct")
    volatility = _metric(result, "realized_vol_20d_pct")

    failed: list[dict[str, Any]] = []
    ratio = close / ma5 if ma5 > 0 else 0.0
    if ratio < 1.02:
        failed.append({"name": "close_vs_ma5_high_vol", "value": round(ratio, 3), "required": ">= 1.02"})
    if not 0.3 <= change <= 8.0:
        failed.append({"name": "change_rate_high_vol", "value": round(change, 2), "required": "0.3~8.0"})
    if return_5d < 1.0:
        failed.append({"name": "return_5d_pct_high_vol", "value": round(return_5d, 2), "required": ">= 1.0"})
    if ma20_slope <= -8.0:
        failed.append({"name": "ma20_slope_pct_high_vol", "value": round(ma20_slope, 2), "required": "> -8.0"})
    if drawdown < -25.0:
        failed.append({"name": "drawdown_20d_pct_high_vol", "value": round(drawdown, 2), "required": ">= -25.0"})
    if not 5.5 <= volatility <= 8.0:
        failed.append({"name": "realized_vol_20d_pct", "value": round(volatility, 2), "required": "5.5~8.0"})
    return failed


def evaluate_market_regime(candles: list[dict[str, Any]], crash_pct: float = -5.0) -> dict[str, Any]:
    """Extend the base regime classifier with a smaller steady-recovery path."""
    result = _base_evaluate_market_regime(candles, crash_pct=crash_pct)
    if _steady_high_vol_recovery(result):
        return {
            **result,
            "status": "volatile_rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.5,
            "failed_conditions": [],
            "reason": (
                "고변동성 완만 회복 확인 — MA5 2% 상회·5일 모멘텀 양수. "
                "주도주 반등형 또는 과매도 반전형만 정상 규모의 50% 허용"
            ),
        }

    if result.get("status") in ("risk_off", "risk_off_selective"):
        volatility = _metric(result, "realized_vol_20d_pct")
        if 5.5 <= volatility <= 8.0:
            existing = [
                item for item in list(result.get("failed_conditions", []) or [])
                if item.get("name") != "realized_vol_20d_pct"
            ]
            high_vol_failed = _high_vol_failed_conditions(result)
            failed = existing + [item for item in high_vol_failed if item not in existing]
            if result.get("buy_allowed", False):
                reason = "60일선 하회 및 고변동성 회복 확인 부족 — 강한 개별 종목만 축소 진입"
            else:
                reason = "60일선 하회 및 고변동성 회복 확인 부족 — 신규 매수 금지"
            if failed:
                details = ", ".join(
                    f"{item['name']}={item['value']} ({item['required']})" for item in failed
                )
                reason = f"{reason} | 차단 조건: {details}"
            return {**result, "failed_conditions": failed, "reason": reason}
    return result


def install() -> None:
    """Replace the runtime classifier without changing broker plumbing."""
    from strategy import runtime

    if getattr(runtime, "_STEADY_RECOVERY_OVERRIDE_INSTALLED", False):
        return
    runtime.evaluate_market_regime = evaluate_market_regime
    runtime._STEADY_RECOVERY_OVERRIDE_INSTALLED = True
