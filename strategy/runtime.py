"""Runtime strategy adapter.

This module keeps broker/API plumbing in ``agent.tools`` while replacing the
market-regime, candidate ranking, technical enrichment, and buy validation
paths with deterministic v9 rules.
"""
from __future__ import annotations

import json
import logging
from datetime import time
from typing import Any

from config import (
    MARKET_CRASH_PCT,
    MARKET_PROXY_TICKER,
    MAX_BUY_AMOUNT,
    MAX_DAILY_BUY_PER_TICKER,
    MAX_POSITION_PCT,
    MAX_POSITIONS,
    MAX_RISK_PER_TRADE_PCT,
    MIN_STOCK_PRICE,
    STOP_LOSS_PCT,
)
from data.trade_log import log_trade
from data.utils import get_now_kst
from strategy.rules import (
    augment_technicals,
    calculate_position_size,
    classify_entry,
    evaluate_market_regime,
)

logger = logging.getLogger(__name__)
_BASE_TOOLS = None
_BASE_EXECUTE_TOOL = None


def install(base_tools) -> None:
    """Install the strategy adapter over agent.tools.execute_tool."""
    global _BASE_TOOLS, _BASE_EXECUTE_TOOL
    if _BASE_TOOLS is base_tools and base_tools.execute_tool is execute_tool:
        return
    _BASE_TOOLS = base_tools
    _BASE_EXECUTE_TOOL = base_tools.execute_tool
    base_tools.execute_tool = execute_tool


def _tools():
    global _BASE_TOOLS, _BASE_EXECUTE_TOOL
    if _BASE_TOOLS is None:
        from agent import tools as imported_tools
        _BASE_TOOLS = imported_tools
        _BASE_EXECUTE_TOOL = imported_tools.execute_tool
    return _BASE_TOOLS


def _enhanced_market_regime(broker, ticker: str = MARKET_PROXY_TICKER) -> dict[str, Any]:
    try:
        candles = broker.get_daily_candles(ticker, days=80)
        result = evaluate_market_regime(candles, crash_pct=MARKET_CRASH_PCT)
        return {"ticker": ticker, **result}
    except Exception as exc:
        logger.warning("향상된 시장 레짐 계산 실패, 기존 로직 폴백: %s", exc)
        return broker.get_market_regime(ticker)


def _enhanced_technicals(broker, ticker: str) -> dict[str, Any]:
    technicals = broker.get_technical_indicators(ticker)
    if technicals.get("error"):
        return technicals
    try:
        candles = broker.get_daily_candles(ticker, days=60)
        current_price = float(technicals.get("current_price", 0) or 0)
        return augment_technicals(technicals, candles, current_price, now=get_now_kst())
    except Exception as exc:
        logger.warning("추가 기술지표 계산 실패 (%s): %s", ticker, exc)
        return {**technicals, "strategy_error": str(exc)}


def _rank_liquid_leaders(broker, n: int) -> list[dict[str, Any]]:
    raw = broker.get_top_volume_stocks(50)
    ranked: list[dict[str, Any]] = []
    for item in raw:
        price = int(item.get("current_price", 0) or 0)
        volume = int(item.get("volume", 0) or 0)
        if price < MIN_STOCK_PRICE:
            continue
        enriched = dict(item)
        enriched["estimated_trading_value"] = price * volume
        ranked.append(enriched)
    ranked.sort(key=lambda row: row.get("estimated_trading_value", 0), reverse=True)
    for index, row in enumerate(ranked, start=1):
        row["liquidity_rank"] = index
    return ranked[:n]


def _buy_stock(tool_input: dict[str, Any]) -> str:
    base_tools = _tools()
    broker = base_tools._broker()
    ticker = str(tool_input["ticker"])
    base_tools._validate_ticker(ticker)
    requested_qty = int(tool_input["quantity"])
    reason = str(tool_input.get("reason", ""))

    if requested_qty <= 0:
        return json.dumps({"success": False, "message": "수량은 1 이상이어야 합니다."}, ensure_ascii=False)

    now = get_now_kst()
    if now.time() < time(9, 10):
        return json.dumps({"success": False, "message": "09:10 이전 신규 매수 금지 — 시초가 변동성 회피"}, ensure_ascii=False)
    if now.time() >= time(15, 20):
        return json.dumps({"success": False, "message": "15:20 이후 신규 매수 금지 — 마감 변동성/미체결 위험 회피"}, ensure_ascii=False)

    if ticker in base_tools._stopped_out_today:
        return json.dumps({"success": False, "message": f"{ticker} 당일 손절 종목 — 재진입 금지"}, ensure_ascii=False)

    buy_count = base_tools._daily_buy_count.get(ticker, 0)
    if buy_count >= MAX_DAILY_BUY_PER_TICKER:
        return json.dumps({
            "success": False,
            "message": f"{ticker} 당일 {buy_count}회 매수 완료 — 일일 한도 초과",
        }, ensure_ascii=False)

    regime = _enhanced_market_regime(broker)
    if not regime.get("buy_allowed", False):
        return json.dumps({
            "success": False,
            "message": f"시장 레짐 {regime.get('status')} — {regime.get('reason')}",
            "market_regime": regime,
        }, ensure_ascii=False)

    technicals = _enhanced_technicals(broker, ticker)
    if technicals.get("error"):
        return json.dumps({
            "success": False,
            "message": f"{ticker} 기술적 지표 확인 실패 — {technicals['error']}",
            "technicals": technicals,
        }, ensure_ascii=False)

    entry = classify_entry(regime, technicals)
    if not entry.get("allowed"):
        return json.dumps({
            "success": False,
            "message": entry.get("reason", "진입 시나리오 미충족"),
            "market_regime": regime,
            "technicals": technicals,
        }, ensure_ascii=False)

    current_price = int(float(technicals.get("current_price", 0) or 0))
    if current_price <= 0:
        current_price = int(broker.get_current_price(ticker)["current_price"])

    if base_tools._is_dry_run():
        if base_tools._sim_portfolio is None:
            base_tools._sim_portfolio = {
                "cash": 1_000_000,
                "holdings": [],
                "holdings_eval": 0,
                "total_eval": 1_000_000,
                "profit_loss": 0,
            }
        portfolio = base_tools._sim_portfolio
    else:
        portfolio = broker.get_balance()

    holdings = portfolio.get("holdings", []) or []
    existing_holding = next((holding for holding in holdings if holding.get("ticker") == ticker), None)
    if existing_holding is None and len(holdings) >= MAX_POSITIONS:
        return json.dumps({
            "success": False,
            "message": f"최대 보유 종목 수 {MAX_POSITIONS}개 도달 — 신규 종목 매수 금지",
        }, ensure_ascii=False)
    existing_position_value = 0
    if existing_holding:
        existing_position_value = int(existing_holding.get("quantity", 0) or 0) * int(
            existing_holding.get("current_price", current_price) or current_price
        )

    sizing = calculate_position_size(
        price=current_price,
        cash=float(portfolio.get("cash", 0) or 0),
        total_eval=float(portfolio.get("total_eval", 0) or 0),
        holdings_eval=float(portfolio.get("holdings_eval", 0) or 0),
        atr_pct=float(technicals.get("atr14_pct", 0) or 0),
        regime_scale=float(regime.get("recommended_buy_scale", 0) or 0),
        setup_scale=float(entry.get("setup_scale", 0) or 0),
        max_buy_amount=MAX_BUY_AMOUNT,
        existing_position_value=existing_position_value,
        risk_per_trade_pct=MAX_RISK_PER_TRADE_PCT,
        max_position_pct=MAX_POSITION_PCT,
        hard_stop_pct=STOP_LOSS_PCT,
    )
    approved_qty = min(requested_qty, int(sizing.get("quantity", 0) or 0))
    if approved_qty < 1:
        return json.dumps({
            "success": False,
            "message": (
                f"리스크 기준상 매수 가능 수량 0주 — 현재가 {current_price:,}원, "
                f"최대 허용금액 {sizing.get('max_amount', 0):,}원"
            ),
            "entry": entry,
            "sizing": sizing,
        }, ensure_ascii=False)

    stock_name = str(technicals.get("name", "") or ticker)
    total_cost = current_price * approved_qty
    final_reason = f"[{entry['setup']}] {reason}".strip()

    if base_tools._is_dry_run():
        available = int(base_tools._sim_portfolio.get("cash", 0) or 0)
        if total_cost > available:
            result = {"success": False, "message": f"가상 예수금 부족: {available:,}원 < {total_cost:,}원"}
        else:
            base_tools._sim_portfolio["cash"] = available - total_cost
            holdings = base_tools._sim_portfolio.setdefault("holdings", [])
            existing = next((holding for holding in holdings if holding["ticker"] == ticker), None)
            if existing:
                previous_qty = int(existing["quantity"])
                new_qty = previous_qty + approved_qty
                existing["avg_price"] = round(
                    (float(existing["avg_price"]) * previous_qty + current_price * approved_qty) / new_qty
                )
                existing["quantity"] = new_qty
                existing["current_price"] = current_price
            else:
                holdings.append({
                    "ticker": ticker,
                    "name": stock_name,
                    "quantity": approved_qty,
                    "avg_price": current_price,
                    "current_price": current_price,
                    "profit_loss_rate": 0.0,
                    "profit_loss_amt": 0,
                })
            base_tools._refresh_sim_portfolio_totals()
            result = {
                "success": True,
                "order_no": "DRY-RUN",
                "message": f"[시뮬레이션] {ticker} {approved_qty}주 매수 @ {current_price:,}원",
                "dry_run": True,
            }
            log_trade(
                "BUY", ticker, approved_qty, current_price, f"[DRY-RUN] {final_reason}", False,
                stock_name, bb_signal=str(technicals.get("bb_position", "")),
                rsi_value=float(technicals.get("rsi", 0) or 0),
            )
    else:
        result = broker.buy_order(ticker, approved_qty)
        if result.get("success"):
            log_trade(
                "BUY", ticker, approved_qty, current_price, final_reason, True,
                stock_name, bb_signal=str(technicals.get("bb_position", "")),
                rsi_value=float(technicals.get("rsi", 0) or 0),
            )

    result.update({
        "reason": final_reason,
        "requested_quantity": requested_qty,
        "approved_quantity": approved_qty,
        "total_cost": total_cost,
        "entry": entry,
        "sizing": sizing,
        "market_regime": regime,
        "technicals": {
            key: technicals.get(key)
            for key in (
                "bb_position", "rsi", "weekly_trend", "return_5d_pct", "return_20d_pct",
                "breakout_pct", "volume_pace_ratio", "atr14_pct", "ma5", "ma20"
            )
        },
    })

    if result.get("success"):
        base_tools._daily_buy_count[ticker] = buy_count + 1
        logger.info(
            "전략 v9 매수 승인: %s %d주 setup=%s scale=%s",
            ticker, approved_qty, entry.get("setup"), sizing.get("effective_scale"),
        )
    return json.dumps(result, ensure_ascii=False)


def execute_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Execute enhanced strategy tools, delegating unrelated tools to the base module."""
    if tool_name.startswith("default_"):
        tool_name = tool_name[len("default_"):]

    try:
        base_tools = _tools()
        broker = base_tools._broker()
        if tool_name == "get_market_regime":
            ticker = str(tool_input.get("ticker") or MARKET_PROXY_TICKER)
            base_tools._validate_ticker(ticker)
            return json.dumps(_enhanced_market_regime(broker, ticker), ensure_ascii=False)
        if tool_name == "get_technical_indicators":
            ticker = str(tool_input["ticker"])
            base_tools._validate_ticker(ticker)
            return json.dumps(_enhanced_technicals(broker, ticker), ensure_ascii=False)
        if tool_name == "get_top_volume_stocks":
            n = min(int(tool_input.get("n", 20)), 50)
            return json.dumps(_rank_liquid_leaders(broker, n), ensure_ascii=False)
        if tool_name == "buy_stock":
            return _buy_stock(tool_input)
        if _BASE_EXECUTE_TOOL is None:
            raise RuntimeError("기존 execute_tool이 설치되지 않았습니다")
        return _BASE_EXECUTE_TOOL(tool_name, tool_input)
    except Exception as exc:
        logger.exception("전략 v9 tool 실행 실패: %s", tool_name)
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
