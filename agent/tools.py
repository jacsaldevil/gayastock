"""Gemini function calling 도구 정의 및 실행 핸들러"""
import json
import os
import re
from vertexai.generative_models import Tool, FunctionDeclaration
from data.trade_log import log_trade

def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "false").lower() == "true"

# KISBroker 싱글턴 — financial.py와 동일 인스턴스 공유
from data.financial import _get_broker as _get_kis_broker

def _broker():
    return _get_kis_broker()


# ── 가상 포트폴리오 상태 (dry-run 시뮬레이션용) ─────────────────
_sim_portfolio: dict | None = None


def set_sim_portfolio(portfolio: dict | None) -> None:
    global _sim_portfolio
    _sim_portfolio = portfolio


def get_sim_portfolio() -> dict | None:
    return _sim_portfolio


def _validate_ticker(ticker: str):
    if not re.fullmatch(r"\d{6}", str(ticker)):
        raise ValueError(f"유효하지 않은 종목코드: {ticker!r} (6자리 숫자여야 합니다)")


# Vertex AI Tool 정의 (dict 기반 — SDK 버전 무관하게 동작)
GEMINI_TOOLS = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="get_stock_price",
            description=(
                "주식 현재가 및 기본 투자지표(PER, PBR, EPS, 등락률)를 조회합니다. "
                "매수/매도 판단 전에 반드시 호출하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "6자리 종목코드 (예: 005930)"},
                },
                "required": ["ticker"],
            },
        ),
        FunctionDeclaration(
            name="get_portfolio",
            description="현재 보유 종목, 수익률, 예수금(현금) 잔고를 조회합니다.",
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="buy_stock",
            description=(
                "주식을 시장가로 매수합니다. "
                "매수 전 get_portfolio로 예수금, get_stock_price로 현재가를 반드시 확인하세요. "
                "포지션 사이징: 가용예수금 × HA강도 비율 ÷ 남은 슬롯 수로 계산하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker":   {"type": "string",  "description": "6자리 종목코드"},
                    "quantity": {"type": "integer", "description": "매수 수량 (주)"},
                    "reason":   {"type": "string",  "description": "매수 근거 (재무지표 수치 포함)"},
                },
                "required": ["ticker", "quantity", "reason"],
            },
        ),
        FunctionDeclaration(
            name="get_top_volume_stocks",
            description=(
                "현재 시장 거래량 상위 종목을 조회합니다. "
                "반환된 목록에서 ETF/스팩/리츠 등을 제외한 후 상위 10종목을 분석 대상으로 선정하세요. "
                "n=30을 사용하세요 (필터 후 10종목 확보를 위해 여유있게 조회)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "조회할 종목 수 (기본 20)"},
                },
            },
        ),
        FunctionDeclaration(
            name="get_heikin_ashi_candles",
            description=(
                "3분봉 하이킨아시 캔들을 조회합니다. "
                "각 캔들에 ha_open/ha_high/ha_low/ha_close, bullish 여부, "
                "upper_wick/lower_wick 크기, 패턴 설명(강한상승/상승저항/강한하락/하락저지)이 포함됩니다. "
                "매수 전 진입 타이밍 판단에 활용하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "6자리 종목코드"},
                },
                "required": ["ticker"],
            },
        ),
        FunctionDeclaration(
            name="sell_stock",
            description="보유 종목을 시장가로 매도합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "ticker":   {"type": "string",  "description": "6자리 종목코드"},
                    "quantity": {"type": "integer", "description": "매도 수량 (주)"},
                    "reason":   {"type": "string",  "description": "매도 근거"},
                },
                "required": ["ticker", "quantity", "reason"],
            },
        ),
    ]
)


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Gemini가 호출한 function을 실행하고 결과를 JSON 문자열로 반환"""
    try:
        broker = _broker()

        if tool_name == "get_stock_price":
            _validate_ticker(tool_input["ticker"])
            result = broker.get_current_price(tool_input["ticker"])

        elif tool_name == "get_top_volume_stocks":
            n = int(tool_input.get("n", 20))
            result = broker.get_top_volume_stocks(n)

        elif tool_name == "get_heikin_ashi_candles":
            _validate_ticker(tool_input["ticker"])
            result = broker.get_minute_candles(tool_input["ticker"])

        elif tool_name == "get_portfolio":
            if _is_dry_run():
                global _sim_portfolio
                if _sim_portfolio is None:
                    # 시뮬레이션 초기 자본 고정 (실제 모의계좌 잔고 무관)
                    _sim_portfolio = {"cash": 1_000_000, "holdings": [], "total_eval": 1_000_000, "profit_loss": 0}
                result = _sim_portfolio
            else:
                result = broker.get_balance()

        elif tool_name == "buy_stock":
            ticker = tool_input["ticker"]
            _validate_ticker(ticker)
            qty = int(tool_input["quantity"])
            reason = tool_input.get("reason", "")

            if qty <= 0:
                return json.dumps({"success": False, "message": "수량은 1 이상이어야 합니다."}, ensure_ascii=False)

            price_info = broker.get_current_price(ticker)
            current_price = price_info["current_price"]
            stock_name = price_info.get("name", ticker)
            total_cost = current_price * qty

            if _is_dry_run():
                if _sim_portfolio is not None:
                    available = _sim_portfolio.get("cash", 0)
                    if total_cost > available:
                        result = {
                            "success": False,
                            "message": f"가상 예수금 부족: 보유 {available:,}원 < 필요 {total_cost:,}원",
                        }
                    else:
                        _sim_portfolio["cash"] = available - total_cost
                        holdings = _sim_portfolio.setdefault("holdings", [])
                        existing = next((h for h in holdings if h["ticker"] == ticker), None)
                        if existing:
                            prev_qty = existing["quantity"]
                            prev_avg = existing["avg_price"]
                            new_qty = prev_qty + qty
                            existing["avg_price"] = round((prev_avg * prev_qty + current_price * qty) / new_qty)
                            existing["quantity"] = new_qty
                            existing["current_price"] = current_price
                            existing["profit_loss_rate"] = round(
                                (current_price - existing["avg_price"]) / existing["avg_price"] * 100, 2
                            )
                        else:
                            holdings.append({
                                "ticker": ticker,
                                "name": stock_name,
                                "quantity": qty,
                                "avg_price": current_price,
                                "current_price": current_price,
                                "profit_loss_rate": 0.0,
                            })
                        result = {
                            "success": True,
                            "order_no": "DRY-RUN",
                            "message": (
                                f"[시뮬레이션] 매수 {ticker} {qty}주 @ {current_price:,}원 = {total_cost:,}원 "
                                f"(가상포트폴리오 반영 — 잔여 예수금 {_sim_portfolio['cash']:,}원)"
                            ),
                            "reason": reason,
                            "total_cost": total_cost,
                            "dry_run": True,
                        }
                        log_trade("BUY", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name)
                else:
                    result = {
                        "success": True,
                        "order_no": "DRY-RUN",
                        "message": f"[시뮬레이션] 매수 {ticker} {qty}주 @ {current_price:,}원 = {total_cost:,}원 (실제 주문 없음)",
                        "reason": reason,
                        "total_cost": total_cost,
                        "dry_run": True,
                    }
                    log_trade("BUY", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name)
            else:
                result = broker.buy_order(ticker, qty)
                result["reason"] = reason
                result["total_cost"] = total_cost
                if result["success"]:
                    log_trade("BUY", ticker, qty, current_price, reason, True, stock_name)

        elif tool_name == "sell_stock":
            ticker = tool_input["ticker"]
            _validate_ticker(ticker)
            qty = int(tool_input["quantity"])
            reason = tool_input.get("reason", "")

            if qty <= 0:
                return json.dumps({"success": False, "message": "수량은 1 이상이어야 합니다."}, ensure_ascii=False)

            # 보유 수량 사전 검증 — dry-run은 가상 포트폴리오에서 확인
            if _is_dry_run() and _sim_portfolio is not None:
                balance = _sim_portfolio
            else:
                balance = broker.get_balance()

            holding = next((h for h in balance["holdings"] if h["ticker"] == ticker), None)
            if not holding:
                return json.dumps({"success": False, "message": f"{ticker} 미보유 종목입니다."}, ensure_ascii=False)
            if qty > holding["quantity"]:
                return json.dumps({
                    "success": False,
                    "message": f"매도 수량({qty}주)이 보유 수량({holding['quantity']}주)을 초과합니다.",
                }, ensure_ascii=False)

            price_info = broker.get_current_price(ticker)
            current_price = price_info["current_price"]
            stock_name = price_info.get("name", ticker)

            if _is_dry_run():
                if _sim_portfolio is not None:
                    proceeds = current_price * qty
                    _sim_portfolio["cash"] = _sim_portfolio.get("cash", 0) + proceeds
                    h = next((x for x in _sim_portfolio["holdings"] if x["ticker"] == ticker), None)
                    if h:
                        if h["quantity"] <= qty:
                            _sim_portfolio["holdings"].remove(h)
                        else:
                            h["quantity"] -= qty
                    message = (
                        f"[시뮬레이션] 매도 {ticker} {qty}주 @ {current_price:,}원 = {proceeds:,}원 "
                        f"(가상포트폴리오 반영 — 잔여 예수금 {_sim_portfolio['cash']:,}원)"
                    )
                else:
                    message = f"[시뮬레이션] 매도 {ticker} {qty}주 @ {current_price:,}원 = {current_price * qty:,}원 (실제 주문 없음)"
                result = {
                    "success": True,
                    "order_no": "DRY-RUN",
                    "message": message,
                    "reason": reason,
                    "dry_run": True,
                }
                log_trade("SELL", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name)
            else:
                result = broker.sell_order(ticker, qty)
                result["reason"] = reason
                if result["success"]:
                    log_trade("SELL", ticker, qty, current_price, reason, True, stock_name)

        else:
            result = {"error": f"알 수 없는 tool: {tool_name}"}

    except Exception as e:
        result = {"error": str(e)}

    return json.dumps(result, ensure_ascii=False)
