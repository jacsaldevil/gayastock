"""Gemini function calling 도구 정의 및 실행 핸들러"""
import json
import logging
import os
import re
from vertexai.generative_models import Tool, FunctionDeclaration
from data.trade_log import log_trade
from config import VWAP_MIN_ENTRY_PCT, VWAP_MAX_ENTRY_PCT, MAX_DAILY_BUY_PER_TICKER

logger = logging.getLogger(__name__)

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


# ── 당일 손절 블락 / 일일 매수 횟수 (코드 레벨 강제) ────────────────
_stopped_out_today: set[str] = set()
_daily_buy_count: dict[str, int] = {}


def _is_stoploss_reason(reason: str) -> bool:
    """손절 매도 여부 판단 — reason 문구(손절/SL:)에 관계없이 일관 적용"""
    r = reason.replace("[DRY-RUN] ", "")
    return "손절" in r or r.startswith("SL") or "stop" in r.lower()


def load_stopped_out_today() -> None:
    """세션 시작 시 오늘 매매 이력을 trade_log에서 읽어 손절 블락 / 일일 매수 횟수 초기화."""
    global _stopped_out_today, _daily_buy_count
    _stopped_out_today = set()
    _daily_buy_count = {}
    try:
        from data.trade_log import get_trades
        from data.utils import get_now_kst
        today = get_now_kst().date().isoformat()
        for t in get_trades(limit=500):
            if t.get("ts", "")[:10] != today:
                continue
            if t.get("action") == "BUY" and t.get("success", True):
                tk = t["ticker"]
                _daily_buy_count[tk] = _daily_buy_count.get(tk, 0) + 1
            elif t.get("action") == "SELL" and _is_stoploss_reason(t.get("reason", "")):
                _stopped_out_today.add(t["ticker"])
        if _stopped_out_today:
            logger.info("당일 손절 블락 로드: %s", ", ".join(sorted(_stopped_out_today)))
        if _daily_buy_count:
            logger.info("당일 매수 횟수 로드: %s",
                        ", ".join(f"{k}={v}회" for k, v in sorted(_daily_buy_count.items())))
    except Exception as e:
        logger.warning("당일 매매 상태 로드 실패: %s", e)


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
                "매수 전 get_portfolio로 예수금, get_heikin_ashi_candles로 VWAP·HA를 반드시 확인하세요. "
                "포지션 사이징: 가용예수금 × HA강도 비율 ÷ 남은 슬롯 수로 계산하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker":      {"type": "string",  "description": "6자리 종목코드"},
                    "quantity":    {"type": "integer", "description": "매수 수량 (주)"},
                    "reason":      {"type": "string",  "description": "매수 근거 (VWAP 이탈률, HA 패턴, 거래량 순위 포함)"},
                    "vwap_dev":    {"type": "number",  "description": "매수 시점 VWAP 이탈률 (%) — get_heikin_ashi_candles의 vwap_deviation_pct"},
                    "ha_pattern":  {"type": "string",  "description": "매수 시점 HA 패턴 — 예: 강한상승, 일반양봉, 음봉 등"},
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
                "3분봉 하이킨아시 캔들과 VWAP을 조회합니다. "
                "반환값: candles(HA 캔들 목록), vwap(VWAP 가격), vwap_deviation_pct(이탈률%), current_price. "
                "vwap_deviation_pct = (현재가-VWAP)/VWAP×100. "
                "0~+3%: 관성 일치(최적 진입), +3% 초과: 고점 주의, 음수: VWAP 미돌파. "
                "HA 패턴과 VWAP 이탈률을 함께 판단하세요."
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
            name="get_financial_summary",
            description=(
                "종목의 연간 재무 요약을 조회합니다 (최근 4개 연도). "
                "반환값: revenue(매출), operating_profit(영업이익), net_profit(순이익), "
                "operating_margin_pct(영업이익률%), roe_pct(ROE%), debt_ratio_pct(부채비율%), "
                "per, pbr, eps. "
                "매수 전 재무 건전성과 성장성을 파악하고 싶을 때 호출하세요."
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
            name="get_daily_price_chart",
            description=(
                "일봉 차트 데이터를 조회합니다. "
                "반환값: 날짜별 open/high/low/close/volume/change_rate 목록. "
                "중장기 추세, 지지/저항 구간, 최근 급등 여부 파악에 사용하세요. "
                "days 파라미터로 조회 기간을 조정할 수 있습니다 (기본 60거래일)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "6자리 종목코드"},
                    "days":   {"type": "integer", "description": "조회할 거래일 수 (기본 20, 최대 60)"},
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
                    "ticker":      {"type": "string",  "description": "6자리 종목코드"},
                    "quantity":    {"type": "integer", "description": "매도 수량 (주)"},
                    "reason":      {"type": "string",  "description": "매도 근거 (TP/SL/VWAP음수/강제청산 중 명시)"},
                    "vwap_dev":    {"type": "number",  "description": "매도 시점 VWAP 이탈률 (%) — get_heikin_ashi_candles의 vwap_deviation_pct"},
                    "ha_pattern":  {"type": "string",  "description": "매도 시점 HA 패턴"},
                },
                "required": ["ticker", "quantity", "reason"],
            },
        ),
    ]
)


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Gemini가 호출한 function을 실행하고 결과를 JSON 문자열로 반환"""
    # Vertex AI SDK가 간혹 'default_' 접두어를 붙여 함수명을 변형하는 버그 대응
    if tool_name.startswith("default_"):
        tool_name = tool_name[len("default_"):]
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

        elif tool_name == "get_financial_summary":
            _validate_ticker(tool_input["ticker"])
            result = broker.get_financial_summary(tool_input["ticker"])

        elif tool_name == "get_daily_price_chart":
            _validate_ticker(tool_input["ticker"])
            days = min(int(tool_input.get("days", 20)), 60)
            result = broker.get_daily_candles(tool_input["ticker"], days)

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
            vwap_dev = tool_input.get("vwap_dev")
            ha_pattern = tool_input.get("ha_pattern", "")

            if qty <= 0:
                return json.dumps({"success": False, "message": "수량은 1 이상이어야 합니다."}, ensure_ascii=False)

            if ticker in _stopped_out_today:
                logger.info("당일 손절 블락 차단: %s", ticker)
                return json.dumps({
                    "success": False,
                    "message": f"{ticker} 당일 손절 종목 — 재진입 금지 (반복 손실 방지)",
                }, ensure_ascii=False)

            # VWAP 이탈률 가드레일 (코드 레벨)
            if vwap_dev is not None:
                if vwap_dev < VWAP_MIN_ENTRY_PCT:
                    logger.info("VWAP 이탈률 부족 차단: %s (%.2f%% < +%.1f%%)", ticker, vwap_dev, VWAP_MIN_ENTRY_PCT)
                    return json.dumps({
                        "success": False,
                        "message": (f"{ticker} VWAP 이탈률 {vwap_dev:+.2f}% — "
                                    f"최소 +{VWAP_MIN_ENTRY_PCT:.1f}% 미만 진입 금지 (모멘텀 부족)"),
                    }, ensure_ascii=False)
                if vwap_dev > VWAP_MAX_ENTRY_PCT:
                    logger.info("VWAP 과추격 차단: %s (%.2f%% > +%.1f%%)", ticker, vwap_dev, VWAP_MAX_ENTRY_PCT)
                    return json.dumps({
                        "success": False,
                        "message": (f"{ticker} VWAP 이탈률 {vwap_dev:+.2f}% — "
                                    f"+{VWAP_MAX_ENTRY_PCT:.1f}% 초과 진입 금지 (고점 추격)"),
                    }, ensure_ascii=False)
            else:
                logger.warning("buy_stock: vwap_dev 미제공 — VWAP 가드레일 미적용 (%s)", ticker)

            # 종목당 일일 최대 매수 횟수 체크
            buy_count = _daily_buy_count.get(ticker, 0)
            if buy_count >= MAX_DAILY_BUY_PER_TICKER:
                logger.info("일일 매수 한도 초과 차단: %s (%d/%d회)", ticker, buy_count, MAX_DAILY_BUY_PER_TICKER)
                return json.dumps({
                    "success": False,
                    "message": (f"{ticker} 당일 {buy_count}회 매수 완료 — "
                                f"최대 {MAX_DAILY_BUY_PER_TICKER}회 초과 금지"),
                }, ensure_ascii=False)

            price_info = broker.get_current_price(ticker)
            current_price = price_info["current_price"]
            stock_name = price_info.get("name", "") or ticker
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
                        log_trade("BUY", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name, vwap_dev=vwap_dev, ha_pattern=ha_pattern)
                else:
                    result = {
                        "success": True,
                        "order_no": "DRY-RUN",
                        "message": f"[시뮬레이션] 매수 {ticker} {qty}주 @ {current_price:,}원 = {total_cost:,}원 (실제 주문 없음)",
                        "reason": reason,
                        "total_cost": total_cost,
                        "dry_run": True,
                    }
                    log_trade("BUY", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name, vwap_dev=vwap_dev, ha_pattern=ha_pattern)
            else:
                result = broker.buy_order(ticker, qty)
                result["reason"] = reason
                result["total_cost"] = total_cost
                if result["success"]:
                    log_trade("BUY", ticker, qty, current_price, reason, True, stock_name, vwap_dev=vwap_dev, ha_pattern=ha_pattern)

            # 성공 시 일일 매수 횟수 업데이트
            if (result or {}).get("success"):
                _daily_buy_count[ticker] = _daily_buy_count.get(ticker, 0) + 1
                logger.info("일일 매수 횟수: %s → %d/%d회", ticker, _daily_buy_count[ticker], MAX_DAILY_BUY_PER_TICKER)

        elif tool_name == "sell_stock":
            ticker = tool_input["ticker"]
            _validate_ticker(ticker)
            qty = int(tool_input["quantity"])
            reason = tool_input.get("reason", "")
            vwap_dev = tool_input.get("vwap_dev")
            ha_pattern = tool_input.get("ha_pattern", "")

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
            stock_name = price_info.get("name", "") or holding.get("name", "") or ticker
            avg_price = holding.get("avg_price", 0)
            realized_profit = int((current_price - avg_price) * qty) if avg_price > 0 else 0

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
                log_trade("SELL", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name, realized_profit, vwap_dev=vwap_dev, ha_pattern=ha_pattern)
            else:
                result = broker.sell_order(ticker, qty)
                result["reason"] = reason
                if result["success"]:
                    log_trade("SELL", ticker, qty, current_price, reason, True, stock_name, realized_profit, vwap_dev=vwap_dev, ha_pattern=ha_pattern)

            # 손절 매도 시 당일 재진입 블락 등록 (dry-run / 실거래 공통)
            if _is_stoploss_reason(reason) and (result or {}).get("success"):
                _stopped_out_today.add(ticker)
                logger.info("당일 손절 블락 등록: %s", ticker)

        else:
            result = {"error": f"알 수 없는 tool: {tool_name}"}

    except Exception as e:
        result = {"error": str(e)}

    return json.dumps(result, ensure_ascii=False)
