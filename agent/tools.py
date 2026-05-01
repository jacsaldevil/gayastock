"""Gemini function calling 도구 정의 및 실행 핸들러"""
import json
import re
import google.generativeai as genai
from data.financial import get_financial_summary
from data.trade_log import log_trade
from config import MAX_BUY_AMOUNT

# KISBroker 싱글턴 — financial.py와 동일 인스턴스 공유
from data.financial import _get_broker as _get_kis_broker

def _broker():
    return _get_kis_broker()


def _validate_ticker(ticker: str):
    if not re.fullmatch(r"\d{6}", str(ticker)):
        raise ValueError(f"유효하지 않은 종목코드: {ticker!r} (6자리 숫자여야 합니다)")


# Gemini FunctionDeclaration 형식
GEMINI_TOOLS = genai.protos.Tool(
    function_declarations=[
        genai.protos.FunctionDeclaration(
            name="get_stock_price",
            description=(
                "주식 현재가 및 기본 투자지표(PER, PBR, EPS, 등락률)를 조회합니다. "
                "매수/매도 판단 전에 반드시 호출하세요."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "ticker": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description="6자리 종목코드 (예: 005930)",
                    ),
                },
                required=["ticker"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="get_financial_statements",
            description=(
                "KIS API 기반 재무제표 요약 조회 (DART 불필요). "
                "매출액, 영업이익, 당기순이익, ROE, 부채비율, 영업이익률, "
                "PER, PBR, 전년 대비 매출 성장률, 최근 3개년 추이 포함."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "ticker": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description="6자리 종목코드",
                    ),
                    "annual": genai.protos.Schema(
                        type=genai.protos.Type.BOOLEAN,
                        description="true=연간(기본), false=분기",
                    ),
                },
                required=["ticker"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="get_portfolio",
            description="현재 보유 종목, 수익률, 예수금(현금) 잔고를 조회합니다.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={},
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="buy_stock",
            description=(
                f"주식을 시장가로 매수합니다. 1회 최대 {MAX_BUY_AMOUNT:,}원 이내. "
                "매수 전 get_portfolio로 예수금, get_stock_price로 현재가를 반드시 확인하세요."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "ticker": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description="6자리 종목코드",
                    ),
                    "quantity": genai.protos.Schema(
                        type=genai.protos.Type.INTEGER,
                        description="매수 수량 (주)",
                    ),
                    "reason": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description="매수 근거 (재무지표 수치 포함)",
                    ),
                },
                required=["ticker", "quantity", "reason"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="sell_stock",
            description="보유 종목을 시장가로 매도합니다.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "ticker": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description="6자리 종목코드",
                    ),
                    "quantity": genai.protos.Schema(
                        type=genai.protos.Type.INTEGER,
                        description="매도 수량 (주)",
                    ),
                    "reason": genai.protos.Schema(
                        type=genai.protos.Type.STRING,
                        description="매도 근거",
                    ),
                },
                required=["ticker", "quantity", "reason"],
            ),
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

        elif tool_name == "get_financial_statements":
            _validate_ticker(tool_input["ticker"])
            result = get_financial_summary(
                tool_input["ticker"],
                annual=tool_input.get("annual", True),
            )

        elif tool_name == "get_portfolio":
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
            total_cost = current_price * qty

            if total_cost > MAX_BUY_AMOUNT:
                result = {
                    "success": False,
                    "message": f"주문금액 {total_cost:,}원이 최대 {MAX_BUY_AMOUNT:,}원 초과",
                }
            else:
                result = broker.buy_order(ticker, qty)
                result["reason"] = reason
                result["total_cost"] = total_cost
                if result["success"]:
                    log_trade("BUY", ticker, qty, current_price, reason, True)

        elif tool_name == "sell_stock":
            ticker = tool_input["ticker"]
            _validate_ticker(ticker)
            qty = int(tool_input["quantity"])
            reason = tool_input.get("reason", "")

            if qty <= 0:
                return json.dumps({"success": False, "message": "수량은 1 이상이어야 합니다."}, ensure_ascii=False)

            # 보유 수량 사전 검증
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
            result = broker.sell_order(ticker, qty)
            result["reason"] = reason
            if result["success"]:
                log_trade("SELL", ticker, qty, current_price, reason, True)

        else:
            result = {"error": f"알 수 없는 tool: {tool_name}"}

    except Exception as e:
        result = {"error": str(e)}

    return json.dumps(result, ensure_ascii=False)
