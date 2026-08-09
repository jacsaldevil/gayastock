"""Pure strategy rules for regime detection, entry validation, and position sizing."""
from __future__ import annotations

from datetime import datetime, time
from statistics import pstdev
from typing import Any


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct_change(current: float, previous: float) -> float:
    return (current - previous) / previous * 100 if previous else 0.0


def evaluate_market_regime(candles: list[dict[str, Any]], crash_pct: float = -5.0) -> dict[str, Any]:
    """Classify the market using trend, momentum, drawdown, and realized volatility.

    Only a severe same-day crash remains a hard block. Recent drawdowns and a
    falling 60-day trend still reduce position size, but they keep candidate
    discovery open so individually recovering leaders can trade selectively.
    """
    if len(candles) < 60:
        return {
            "status": "unknown",
            "buy_allowed": False,
            "recommended_buy_scale": 0.0,
            "reason": f"시장 데이터 부족: {len(candles)}개",
        }

    closes = [float(c.get("close", 0) or 0) for c in candles if float(c.get("close", 0) or 0) > 0]
    if len(closes) < 60:
        return {
            "status": "unknown",
            "buy_allowed": False,
            "recommended_buy_scale": 0.0,
            "reason": f"유효 종가 데이터 부족: {len(closes)}개",
        }

    close = closes[-1]
    ma5 = _mean(closes[-5:])
    ma20 = _mean(closes[-20:])
    ma60 = _mean(closes[-60:])
    previous_ma20 = _mean(closes[-25:-5]) if len(closes) >= 25 else ma20
    ma20_slope_pct = _pct_change(ma20, previous_ma20)
    return_5d_pct = _pct_change(close, closes[-6]) if len(closes) >= 6 else 0.0
    return_20d_pct = _pct_change(close, closes[-21]) if len(closes) >= 21 else 0.0

    recent_highs = [float(c.get("high", c.get("close", 0)) or 0) for c in candles[-20:]]
    high20 = max(recent_highs) if recent_highs else close
    drawdown_20d_pct = _pct_change(close, high20)

    returns = [_pct_change(closes[i], closes[i - 1]) for i in range(1, len(closes))]
    realized_vol_20d_pct = pstdev(returns[-20:]) if len(returns) >= 20 else 0.0

    latest_change_pct = float(candles[-1].get("change_rate", 0) or 0)
    if not latest_change_pct and len(closes) >= 2:
        latest_change_pct = _pct_change(close, closes[-2])

    metrics = {
        "close": round(close, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "ma20_slope_pct": round(ma20_slope_pct, 2),
        "return_5d_pct": round(return_5d_pct, 2),
        "return_20d_pct": round(return_20d_pct, 2),
        "drawdown_20d_pct": round(drawdown_20d_pct, 2),
        "realized_vol_20d_pct": round(realized_vol_20d_pct, 2),
        "change_rate": round(latest_change_pct, 2),
    }

    # 당일 실제 폭락만 전면 차단한다. 최근 급락 이력만으로 다음 거래일까지
    # 후보 탐색을 통째로 중단하지 않는다.
    if latest_change_pct <= crash_pct:
        return {
            **metrics,
            "status": "crash",
            "buy_allowed": False,
            "recommended_buy_scale": 0.0,
            "reason": f"당일 시장 급락({latest_change_pct:.1f}% <= {crash_pct:.1f}%) — 신규 매수 금지",
        }

    # 고변동성 + 최근 5일 급락은 전면 금지 대신 초소형 선별 진입으로 전환한다.
    # 개별 종목은 classify_entry의 반전/상대강도 조건을 별도로 통과해야 한다.
    recent_shock = realized_vol_20d_pct >= 5.0 and return_5d_pct <= -6.0
    if recent_shock:
        return {
            **metrics,
            "status": "crash_selective",
            "buy_allowed": True,
            "recommended_buy_scale": 0.35,
            "reason": "최근 급락·고변동성 지속 — 후보 탐색 후 반전/상대강도 종목만 정상 규모의 35% 허용",
        }

    # 낮은 변동성의 정석 반등. 5.5% 이상 변동성은 volatile_rebound로 분리한다.
    rebound = (
        close < ma60
        and close > ma5
        and latest_change_pct >= 0.8
        and return_5d_pct >= 0.0
        and ma20_slope_pct > -7.0
        and drawdown_20d_pct >= -30.0
        and realized_vol_20d_pct < 5.5
    )
    if rebound:
        return {
            **metrics,
            "status": "rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.7,
            "reason": "60일선 아래지만 단기 반등 확인 — 선별 진입을 정상 규모의 70% 허용",
        }

    # 2026-07-31처럼 변동성은 높지만 가격·5일 모멘텀이 빠르게 복구되는 V자 반등.
    # 오전 급등 직후 추격하지 않도록 MA5 괴리, 5일 수익률, 낙폭 회복을 함께 요구한다.
    volatile_rebound = (
        close < ma60
        and close >= ma5 * 1.05
        and 2.0 <= latest_change_pct <= 25.0
        and return_5d_pct >= -0.5
        and ma20_slope_pct > -8.0
        and drawdown_20d_pct >= -25.0
        and 5.5 <= realized_vol_20d_pct <= 8.0
    )
    if volatile_rebound:
        return {
            **metrics,
            "status": "volatile_rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.5,
            "reason": (
                "고변동성 V자 반등 확인 — 당일 +8% 이하의 주도주 반등형 또는 "
                "과매도 반전형만 정상 규모의 50% 허용"
            ),
        }

    # 완전한 반등에는 못 미쳐도 단기선 회복과 양의 모멘텀이 있으면 아주 제한적으로 탐색한다.
    selective_risk_off = (
        close < ma60
        and close >= ma5
        and latest_change_pct >= 0.5
        and return_5d_pct >= -2.0
        and ma20_slope_pct > -8.0
        and drawdown_20d_pct >= -30.0
        and realized_vol_20d_pct < 5.5
    )
    if selective_risk_off:
        return {
            **metrics,
            "status": "risk_off_selective",
            "buy_allowed": True,
            "recommended_buy_scale": 0.5,
            "reason": "60일선 아래의 제한적 회복 — 반전/상대강도 종목을 정상 규모의 50% 허용",
        }

    if close < ma60:
        failed_conditions: list[dict[str, Any]] = []
        if close < ma5:
            failed_conditions.append({"name": "close_vs_ma5", "value": round(close / ma5, 3), "required": ">= 1.0"})
        if return_5d_pct < -2.0:
            failed_conditions.append({"name": "return_5d_pct", "value": round(return_5d_pct, 2), "required": ">= -2.0"})
        if ma20_slope_pct <= -8.0:
            failed_conditions.append({"name": "ma20_slope_pct", "value": round(ma20_slope_pct, 2), "required": "> -8.0"})
        if drawdown_20d_pct < -30.0:
            failed_conditions.append({"name": "drawdown_20d_pct", "value": round(drawdown_20d_pct, 2), "required": ">= -30.0"})
        if realized_vol_20d_pct >= 5.5:
            failed_conditions.append({
                "name": "realized_vol_20d_pct",
                "value": round(realized_vol_20d_pct, 2),
                "required": "< 5.5 or volatile_rebound 5.5~8.0",
            })
        reason = "60일선 하회 및 단기 회복 확인 부족 — 후보 탐색 후 강한 개별 종목만 40% 규모 허용"
        if failed_conditions:
            failed_text = ", ".join(
                f"{item['name']}={item['value']} ({item['required']})" for item in failed_conditions
            )
            reason = f"{reason} | 시장 회복 미충족 항목: {failed_text}"
        return {
            **metrics,
            "status": "risk_off_selective",
            "buy_allowed": True,
            "recommended_buy_scale": 0.4,
            "failed_conditions": failed_conditions,
            "reason": reason,
        }

    caution = close < ma20 or ma20_slope_pct < 0 or realized_vol_20d_pct >= 3.5 or return_5d_pct <= -4.0
    if caution:
        return {
            **metrics,
            "status": "caution",
            "buy_allowed": True,
            "recommended_buy_scale": 0.7,
            "reason": "중기 추세는 유지되나 단기 약세/고변동성 — 정상 규모의 70% 허용",
        }

    return {
        **metrics,
        "status": "risk_on",
        "buy_allowed": True,
        "recommended_buy_scale": 1.0,
        "reason": "20일선·60일선 상회, 추세와 변동성 양호 — 정상 규모 허용",
    }


def augment_technicals(
    technicals: dict[str, Any],
    candles: list[dict[str, Any]],
    current_price: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add momentum, ATR, breakout, and intraday volume-pace metrics."""
    result = dict(technicals)
    if len(candles) < 21 or current_price <= 0:
        result.setdefault("strategy_error", f"추가 지표 데이터 부족: {len(candles)}개")
        return result

    closes = [float(c.get("close", 0) or 0) for c in candles]
    highs = [float(c.get("high", 0) or 0) for c in candles]
    lows = [float(c.get("low", 0) or 0) for c in candles]
    volumes = [float(c.get("volume", 0) or 0) for c in candles]

    result["ma5"] = round(_mean(closes[-5:]), 2)
    result["ma20"] = round(_mean(closes[-20:]), 2)
    result["ma60"] = round(_mean(closes[-60:]), 2) if len(closes) >= 60 else None
    result["return_5d_pct"] = round(_pct_change(current_price, closes[-6]), 2)
    result["return_20d_pct"] = round(_pct_change(current_price, closes[-21]), 2)

    previous_20d_high = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[:-1])
    result["previous_20d_high"] = round(previous_20d_high, 2)
    result["breakout_pct"] = round(_pct_change(current_price, previous_20d_high), 2)

    true_ranges: list[float] = []
    for i in range(max(1, len(candles) - 14), len(candles)):
        previous_close = closes[i - 1]
        true_ranges.append(max(highs[i] - lows[i], abs(highs[i] - previous_close), abs(lows[i] - previous_close)))
    atr14 = _mean(true_ranges)
    result["atr14"] = round(atr14, 2)
    result["atr14_pct"] = round(atr14 / current_price * 100, 2) if current_price else 0.0

    average_volume_20 = _mean(volumes[-21:-1])
    latest_volume = volumes[-1]
    now = now or datetime.now()
    latest_date = str(candles[-1].get("date", ""))
    today_key = now.strftime("%Y%m%d")
    if latest_date == today_key and time(9, 0) <= now.time() <= time(15, 30):
        elapsed_minutes = (now.hour * 60 + now.minute) - 9 * 60
        session_fraction = min(1.0, max(0.05, elapsed_minutes / 390))
    else:
        session_fraction = 1.0
    expected_volume = average_volume_20 * session_fraction
    result["volume_pace_ratio"] = round(latest_volume / expected_volume, 2) if expected_volume > 0 else 0.0
    result["above_ma5"] = current_price >= result["ma5"]
    result["above_ma20"] = current_price >= result["ma20"]
    return result


def classify_entry(regime: dict[str, Any], technicals: dict[str, Any]) -> dict[str, Any]:
    """Validate a supported entry setup and return a setup-specific scale."""
    status = str(regime.get("status", "unknown"))
    if not regime.get("buy_allowed", False):
        return {"allowed": False, "setup": None, "setup_scale": 0.0, "reason": regime.get("reason", "시장 매수 금지")}

    bb = str(technicals.get("bb_position", ""))
    rsi = float(technicals.get("rsi", 100) or 100)
    weekly = str(technicals.get("weekly_trend", "unknown"))
    change = float(technicals.get("change_rate", 0) or 0)
    ret5 = float(technicals.get("return_5d_pct", 0) or 0)
    ret20 = float(technicals.get("return_20d_pct", 0) or 0)
    breakout = float(technicals.get("breakout_pct", -100) or -100)
    volume_pace = float(technicals.get("volume_pace_ratio", 0) or 0)
    atr_pct = float(technicals.get("atr14_pct", 100) or 100)
    above_ma5 = bool(technicals.get("above_ma5", False))
    above_ma20 = bool(technicals.get("above_ma20", False))

    # 모든 진입 유형에 공통으로 개별 종목 당일 +8% 초과 추격을 금지한다.
    if change > 8.0:
        return {
            "allowed": False,
            "setup": None,
            "setup_scale": 0.0,
            "reason": f"당일 상승률 {change:.1f}% — +8% 초과 추격 매수 금지",
        }

    momentum_breakout = (
        status in ("risk_on", "caution", "rebound", "volatile_rebound")
        and weekly == "up"
        and bb in ("middle", "upper_touch", "above_upper")
        and 55 <= rsi <= 72
        and breakout >= 0.3
        and volume_pace >= 1.3
        and 0.5 <= change <= 8.0
        and atr_pct <= 7.0
    )
    if momentum_breakout:
        return {"allowed": True, "setup": "momentum_breakout", "setup_scale": 0.7, "reason": "주도주 20일 신고가 돌파 + 거래량 속도 확인"}

    leader_pullback = (
        status in (
            "crash_selective", "risk_off_selective", "rebound",
            "volatile_rebound", "risk_on", "caution",
        )
        and weekly == "up"
        and bb in ("middle", "lower_touch")
        and 38 <= rsi <= 62
        and ret20 >= 3.0
        and -8.0 <= ret5 <= 6.0
        and above_ma20
        and atr_pct <= 7.0
    )
    if leader_pullback:
        return {"allowed": True, "setup": "leader_pullback", "setup_scale": 1.0, "reason": "중기 주도주가 20일선 위에서 건전한 눌림목 형성"}

    # 고변동성 V자 반등장에서는 시장보다 먼저 회복한 종목만 제한적으로 추종한다.
    # 개별 종목 당일 +8% 초과 추격, MA20 미회복, 거래량 없는 반등은 허용하지 않는다.
    volatile_rebound_leader = (
        status == "volatile_rebound"
        and weekly in ("up", "sideways")
        and bb in ("middle", "upper_touch")
        and 48 <= rsi <= 68
        and 0.8 <= change <= 8.0
        and ret5 >= 0.0
        and ret20 >= -3.0
        and breakout >= -5.0
        and volume_pace >= 1.1
        and above_ma5
        and above_ma20
        and atr_pct <= 8.0
    )
    if volatile_rebound_leader:
        return {
            "allowed": True,
            "setup": "volatile_rebound_leader",
            "setup_scale": 0.8,
            "reason": "고변동성 반등장에서 MA20·거래량을 회복한 주도주 확인",
        }

    oversold_reversal = (
        status in (
            "crash_selective", "risk_off_selective", "rebound",
            "volatile_rebound", "caution", "risk_on",
        )
        and weekly in ("up", "sideways", "down")
        and bb in ("below_lower", "lower_touch")
        and 22 <= rsi <= 48
        and (above_ma5 or change >= 0.3)
        and ret5 >= -15.0
        and atr_pct <= 10.0
    )
    if oversold_reversal:
        return {"allowed": True, "setup": "oversold_reversal", "setup_scale": 0.8, "reason": "과매도 구간에서 단기 반전 확인"}

    # 약세장에서도 시장보다 먼저 회복하는 종목은 소규모로 진입한다.
    relative_strength_recovery = (
        status in ("crash_selective", "risk_off_selective", "rebound", "volatile_rebound", "caution")
        and weekly in ("up", "sideways")
        and bb in ("lower_touch", "middle", "upper_touch")
        and 40 <= rsi <= 68
        and 0.3 <= change <= 8.0
        and ret5 >= -3.0
        and volume_pace >= 1.0
        and above_ma5
        and atr_pct <= 9.0
    )
    if relative_strength_recovery:
        return {
            "allowed": True,
            "setup": "relative_strength_recovery",
            "setup_scale": 0.8,
            "reason": "약세장에서도 MA5·거래량·단기 상대강도를 먼저 회복",
        }

    return {
        "allowed": False,
        "setup": None,
        "setup_scale": 0.0,
        "reason": (
            f"진입 시나리오 미충족: regime={status}, bb={bb}, rsi={rsi:.1f}, weekly={weekly}, "
            f"ret5={ret5:.1f}%, ret20={ret20:.1f}%, breakout={breakout:.1f}%, "
            f"volume_pace={volume_pace:.1f}, atr={atr_pct:.1f}%"
        ),
    }


def calculate_position_size(
    *,
    price: float,
    cash: float,
    total_eval: float,
    holdings_eval: float,
    atr_pct: float,
    regime_scale: float,
    setup_scale: float,
    max_buy_amount: float,
    existing_position_value: float = 0.0,
    risk_per_trade_pct: float = 1.5,
    max_position_pct: float = 40.0,
    hard_stop_pct: float = 7.0,
) -> dict[str, Any]:
    """Cap quantity by equity risk, regime scale, setup quality, cash, and max position."""
    if price <= 0:
        return {"quantity": 0, "max_amount": 0, "stop_distance_pct": hard_stop_pct, "reason": "현재가 오류"}

    equity = max(float(total_eval or 0), float(cash or 0) + float(holdings_eval or 0), float(cash or 0))
    effective_scale = max(0.0, min(1.0, float(regime_scale or 0) * float(setup_scale or 0)))
    stop_distance_pct = min(hard_stop_pct, max(4.0, float(atr_pct or 0) * 1.5))
    risk_budget = equity * risk_per_trade_pct / 100
    risk_cap = risk_budget / (stop_distance_pct / 100) if stop_distance_pct > 0 else 0
    target_position_cap = equity * max_position_pct / 100 * effective_scale
    position_cap = max(0.0, target_position_cap - float(existing_position_value or 0))
    cash_cap = float(cash or 0) * effective_scale
    max_amount = max(0.0, min(float(max_buy_amount), risk_cap, position_cap, cash_cap))
    quantity = int(max_amount // price)
    return {
        "quantity": quantity,
        "max_amount": int(max_amount),
        "effective_scale": round(effective_scale, 3),
        "stop_distance_pct": round(stop_distance_pct, 2),
        "risk_budget": int(risk_budget),
    }
