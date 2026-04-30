"""Claude tool_use 정의 및 실행 핸들러"""
import json
from broker.kis import KISBroker
from data.financial import get_financial_summary
from config import MAX_BUY_AMOUNT

broker = KISBroker()

# Claude에게 넘길 tool 스펙 목록
TOOLS = [
    {
        "name": "get_stock_price",
        "description": (
            "주식 현재가 및 기본 투자지표(PER, PBR, EPS, 등락률)를 조회합니다. "
            "매수/매도 판단 전에 반드시 호출하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "6자리 종목코드 (예: 005930)"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_financial_statements",
        "description": (
            "KIS API 기반 재무제표 요약을 가져옵니다 (DART 불필요). "
            "매출액, 영업이익, 당기순이익, ROE, 부채비율, 영업이익률, PER, PBR, "
            "전년 대비 매출 성장률, 최근 3개년 추이를 포함합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "6자리 종목코드"},
                "annual": {
                    "type": "boolean",
                    "description": "true=연간(기본), false=분기",
                    "default": True,
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_portfolio",
        "description": "현재 보유 종목, 수익률, 예수금(현금) 잔고를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "buy_stock",
        "description": (
            f"주식을 시장가로 매수합니다. 1회 최대 {MAX_BUY_AMOUNT:,}원 이내로만 주문하세요. "
            "매수 전 get_portfolio로 예수금을 확인하고, get_stock_price로 현재가를 확인하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "6자리 종목코드"},
                "quantity": {"type": "integer", "description": "매수 수량 (주)"},
                "reason": {"type": "string", "description": "매수 근거 (재무지표 기반 설명)"},
            },
            "required": ["ticker", "quantity", "reason"],
        },
    },
    {
        "name": "sell_stock",
        "description": "보유 종목을 시장가로 매도합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "6자리 종목코드"},
                "quantity": {"type": "integer", "description": "매도 수량 (주)"},
                "reason": {"type": "string", "description": "매도 근거"},
            },
            "required": ["ticker", "quantity", "reason"],
        },
    },
]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Claude가 선택한 tool을 실행하고 결과를 문자열로 반환"""
    try:
        if tool_name == "get_stock_price":
            result = broker.get_current_price(tool_input["ticker"])

        elif tool_name == "get_financial_statements":
            result = get_financial_summary(
                tool_input["ticker"],
                annual=tool_input.get("annual", True),
            )

        elif tool_name == "get_portfolio":
            result = broker.get_balance()

        elif tool_name == "buy_stock":
            price_info = broker.get_current_price(tool_input["ticker"])
            current_price = price_info["current_price"]
            qty = tool_input["quantity"]
            total_cost = current_price * qty

            if total_cost > MAX_BUY_AMOUNT:
                result = {
                    "success": False,
                    "message": f"주문금액 {total_cost:,}원이 최대 허용금액 {MAX_BUY_AMOUNT:,}원을 초과합니다.",
                }
            else:
                result = broker.buy_order(tool_input["ticker"], qty)
                result["reason"] = tool_input.get("reason", "")
                result["total_cost"] = total_cost

        elif tool_name == "sell_stock":
            result = broker.sell_order(tool_input["ticker"], tool_input["quantity"])
            result["reason"] = tool_input.get("reason", "")

        else:
            result = {"error": f"알 수 없는 tool: {tool_name}"}

    except Exception as e:
        result = {"error": str(e)}

    return json.dumps(result, ensure_ascii=False)
