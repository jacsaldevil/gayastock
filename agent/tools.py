"""Gemini function calling 도구 정의 및 실행 핸들러"""
import json
import logging
import os
import re
from vertexai.generative_models import Tool, FunctionDeclaration
from data.trade_log import log_trade
from config import RSI_MAX_ENTRY, MAX_DAILY_BUY_PER_TICKER, MARKET_PROXY_TICKER

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
            # DRY-RUN 매수는 success=False로 기록되지만 일일 한도 추적에는 포함
            is_buy = t.get("action") == "BUY" and (
                t.get("success", True) or "[DRY-RUN]" in t.get("reason", "")
            )
            if is_buy:
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


def _business_days_between(start_date: str, end_date) -> int:
    """KST 기준 보유 영업일 수 계산 (시작일 제외, 종료일 포함)."""
    from datetime import date, timedelta

    try:
        start = date.fromisoformat(start_date)
    except (TypeError, ValueError):
        return 0
    if start >= end_date:
        return 0

    try:
        import holidays
        kr_holidays = holidays.SouthKorea(years=range(start.year, end_date.year + 1))
    except Exception:
        kr_holidays = set()

    days = 0
    cursor = start + timedelta(days=1)
    while cursor <= end_date:
        if cursor.weekday() < 5 and cursor not in kr_holidays:
            days += 1
        cursor += timedelta(days=1)
    return days


def _refresh_sim_portfolio_totals() -> None:
    """dry-run 가상 포트폴리오의 평가액/손익 합계를 현재 보유 상태와 동기화."""
    if _sim_portfolio is None:
        return

    holdings_eval = 0
    profit_loss = 0
    for holding in _sim_portfolio.get("holdings", []):
        quantity = int(holding.get("quantity", 0) or 0)
        current_price = float(holding.get("current_price", 0) or 0)
        avg_price = float(holding.get("avg_price", 0) or 0)
        holding_eval = int(current_price * quantity)
        holdings_eval += holding_eval
        if avg_price > 0:
            holding_profit = int((current_price - avg_price) * quantity)
            holding["profit_loss_amt"] = holding_profit
            holding["profit_loss_rate"] = round((current_price - avg_price) / avg_price * 100, 2)
            profit_loss += holding_profit

    _sim_portfolio["holdings_eval"] = holdings_eval
    _sim_portfolio["total_eval"] = int(_sim_portfolio.get("cash", 0) or 0) + holdings_eval
    _sim_portfolio["profit_loss"] = profit_loss


# Vertex AI Tool 정의 (dict 기반 — SDK 버전 무관하게 동작)
GEMINI_TOOLS = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="get_stock_price",
            description=(
                "주식 현재가 및 기본 투자지표(PER, PBR, EPS, 등락률)를 조회합니다. "
                "BB/RSI 확인 후 최종 진입가 검증 시 호출하세요."
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
            description=(
                "현재 보유 종목, 수익률, 예수금(현금) 잔고를 조회합니다. "
                "hold_days(보유일수)와 buy_date(최초 매수일)가 포함됩니다. "
                "매 실행 시 가장 먼저 호출하세요."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="get_technical_indicators",
            description=(
                "종목의 볼린저밴드(BB, 20일), RSI(14일), 주봉 추세를 조회합니다. "
                "반환값: bb_upper/bb_middle/bb_lower(밴드 가격), bb_position(below_lower/lower_touch/middle/upper_touch/above_upper), "
                "bb_width_pct(밴드 폭%), rsi(RSI값 0~100), weekly_trend(up/down/sideways/unknown), current_price. "
                "매수 판단 전에 반드시 호출하세요. 매도 시에도 BB 상단/RSI 확인에 활용하세요."
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
            name="get_top_volume_stocks",
            description=(
                "현재 시장 거래량 상위 종목을 조회합니다. "
                "반환된 목록에서 ETF/스팩/리츠 등을 제외한 후 후보 종목을 선정하세요. "
                "n=50을 사용하여 충분한 후보군을 확보한 뒤 get_technical_indicators로 BB/RSI를 확인하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "조회할 종목 수 (기본 20, 최대 50 권장)"},
                },
            },
        ),
        FunctionDeclaration(
            name="get_market_regime",
            description=(
                "코스피 대형주 프록시(KODEX 200)로 시장 레짐을 확인합니다. "
                "매수 전 반드시 호출하세요. status는 risk_on/caution/risk_off/crash/unknown 중 하나이며, "
                "buy_allowed=false이면 신규 매수 금지, caution이면 recommended_buy_scale에 맞춰 절반 수량만 매수하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": f"시장 프록시 6자리 코드 (기본 {MARKET_PROXY_TICKER}=KODEX 200)",
                    },
                },
            },
        ),
        FunctionDeclaration(
            name="buy_stock",
            description=(
                "주식을 매수합니다. "
                "매수 전 get_market_regime의 buy_allowed=true와 "
                "get_technical_indicators의 주봉 우상향(up)을 반드시 확인하세요. "
                "진입은 과매도형(BB 하단+RSI≤35) 또는 주도주 눌림목형(BB middle/lower_touch + RSI≤55) 중 하나여야 합니다. "
                "포지션 사이징: risk_on은 가용예수금 × 30~40% ÷ 남은 슬롯 수, caution은 그 절반 이하로 계산하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker":     {"type": "string",  "description": "6자리 종목코드"},
                    "quantity":   {"type": "integer", "description": "매수 수량 (주)"},
                    "reason":     {"type": "string",  "description": "매수 근거 (BB 위치, RSI 값, 주봉 추세, 재무 건전성 포함)"},
                    "bb_signal":  {"type": "string",  "description": "BB 위치 — below_lower/lower_touch/middle/upper_touch/above_upper"},
                    "rsi_value":  {"type": "number",  "description": "매수 시점 RSI 값 (0~100)"},
                },
                "required": ["ticker", "quantity", "reason"],
            },
        ),
        FunctionDeclaration(
            name="get_financial_summary",
            description=(
                "종목의 연간 재무 요약을 조회합니다 (최근 4개 연도). "
                "반환값: revenue(매출), operating_profit(영업이익), net_profit(순이익), "
                "operating_margin_pct(영업이익률%), roe_pct(ROE%), debt_ratio_pct(부채비율%), "
                "per, pbr, eps. "
                "BB/RSI 조건 충족 후 재무 건전성 검증 시 호출하세요."
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
                "일봉 차트 데이터를 조회합니다 (최대 60거래일). "
                "지지/저항 구간, 52주 고저점, 최근 급락 원인 파악에 활용하세요."
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
            description=(
                "보유 종목을 매도합니다. "
                "매도 사유: BB 상단 근접/이탈(upper_touch/above_upper), RSI≥70, "
                "목표가(+8%) 달성, 손절(-5%), 보유기간 초과(10영업일) 중 명시하세요."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ticker":     {"type": "string",  "description": "6자리 종목코드"},
                    "quantity":   {"type": "integer", "description": "매도 수량 (주)"},
                    "reason":     {"type": "string",  "description": "매도 근거 (TP/SL/BB상단/RSI과매수/보유기간초과 중 명시)"},
                    "bb_signal":  {"type": "string",  "description": "매도 시점 BB 위치"},
                    "rsi_value":  {"type": "number",  "description": "매도 시점 RSI 값"},
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
            n = min(int(tool_input.get("n", 20)), 50)
            result = broker.get_top_volume_stocks(n)

        elif tool_name == "get_technical_indicators":
            _validate_ticker(tool_input["ticker"])
            result = broker.get_technical_indicators(tool_input["ticker"])

        elif tool_name == "get_market_regime":
            ticker = tool_input.get("ticker") or MARKET_PROXY_TICKER
            _validate_ticker(ticker)
            result = broker.get_market_regime(ticker)

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
                    _sim_portfolio = {
                        "cash": 1_000_000,
                        "holdings": [],
                        "holdings_eval": 0,
                        "total_eval": 1_000_000,
                        "profit_loss": 0,
                    }
                _refresh_sim_portfolio_totals()
                result = _sim_portfolio
            else:
                result = broker.get_balance()

            # 보유기간(hold_days) 계산 — 스윙 트레이딩 최대 보유일 초과 판단용
            try:
                from data.trade_log import get_trades
                from data.utils import get_now_kst
                today = get_now_kst().date()
                # 오래된 순으로 정렬해 포지션 진입일 추적
                all_trades = sorted(get_trades(limit=1000), key=lambda t: t.get("ts", ""))
                position_open_date: dict[str, str] = {}
                for t in all_trades:
                    tk = t.get("ticker", "")
                    if not tk:
                        continue
                    is_buy = t.get("action") == "BUY" and (
                        t.get("success") or "[DRY-RUN]" in t.get("reason", "")
                    )
                    is_sell = t.get("action") == "SELL" and (
                        t.get("success") or "[DRY-RUN]" in t.get("reason", "")
                    )
                    if is_buy and tk not in position_open_date:
                        position_open_date[tk] = t.get("ts", "")[:10]
                    elif is_sell:
                        position_open_date.pop(tk, None)
                for h in result.get("holdings", []):
                    tk = h["ticker"]
                    if tk in position_open_date:
                        h["hold_days"] = _business_days_between(position_open_date[tk], today)
                        h["buy_date"] = position_open_date[tk]
                    else:
                        h["hold_days"] = 0
                        h["buy_date"] = "unknown"
            except Exception as e:
                logger.warning("보유기간 계산 실패: %s", e)

        elif tool_name == "buy_stock":
            ticker = tool_input["ticker"]
            _validate_ticker(ticker)
            qty = int(tool_input["quantity"])
            reason = tool_input.get("reason", "")
            bb_signal = tool_input.get("bb_signal", "")
            rsi_value = tool_input.get("rsi_value")

            if qty <= 0:
                return json.dumps({"success": False, "message": "수량은 1 이상이어야 합니다."}, ensure_ascii=False)

            if ticker in _stopped_out_today:
                logger.info("당일 손절 블락 차단: %s", ticker)
                return json.dumps({
                    "success": False,
                    "message": f"{ticker} 당일 손절 종목 — 재진입 금지 (반복 손실 방지)",
                }, ensure_ascii=False)

            # RSI 과열 가드레일 (코드 레벨) — RSI > RSI_MAX_ENTRY이면 진입 금지
            if rsi_value is not None and rsi_value > RSI_MAX_ENTRY:
                logger.info("RSI 진입 차단: %s (RSI %.1f > %.1f)", ticker, rsi_value, RSI_MAX_ENTRY)
                return json.dumps({
                    "success": False,
                    "message": (f"{ticker} RSI {rsi_value:.1f} — "
                                f"{RSI_MAX_ENTRY:.0f} 초과 시 매수 금지 (과열 진입 방지)"),
                }, ensure_ascii=False)
            elif rsi_value is None:
                logger.warning("buy_stock: rsi_value 미제공 — RSI 가드레일 미적용 (%s)", ticker)

            # BB 상단 가드레일
            if bb_signal in ("upper_touch", "above_upper"):
                logger.info("BB 상단 진입 차단: %s (bb_signal=%s)", ticker, bb_signal)
                return json.dumps({
                    "success": False,
                    "message": f"{ticker} BB 상단 근접/이탈({bb_signal}) — 매수 금지 (고점 진입)",
                }, ensure_ascii=False)

            # 종목당 일일 최대 매수 횟수 체크
            buy_count = _daily_buy_count.get(ticker, 0)
            if buy_count >= MAX_DAILY_BUY_PER_TICKER:
                logger.info("일일 매수 한도 초과 차단: %s (%d/%d회)", ticker, buy_count, MAX_DAILY_BUY_PER_TICKER)
                return json.dumps({
                    "success": False,
                    "message": (f"{ticker} 당일 {buy_count}회 매수 완료 — "
                                f"최대 {MAX_DAILY_BUY_PER_TICKER}회 초과 금지"),
                }, ensure_ascii=False)

            market_regime = broker.get_market_regime()
            if not market_regime.get("buy_allowed", False):
                logger.info("시장 레짐 매수 차단: %s", market_regime)
                return json.dumps({
                    "success": False,
                    "message": f"시장 레짐 {market_regime.get('status')} — {market_regime.get('reason')}",
                    "market_regime": market_regime,
                }, ensure_ascii=False)

            technicals = broker.get_technical_indicators(ticker)
            if technicals.get("error"):
                return json.dumps({
                    "success": False,
                    "message": f"{ticker} 기술적 지표 확인 실패 — {technicals['error']}",
                    "technicals": technicals,
                }, ensure_ascii=False)
            actual_bb = technicals.get("bb_position", "")
            actual_rsi = float(technicals.get("rsi", 100) or 100)
            weekly_trend = technicals.get("weekly_trend", "unknown")
            regime_status = market_regime.get("status")

            deep_pullback = (
                regime_status in ("caution", "risk_on")
                and actual_bb in ("below_lower", "lower_touch")
                and actual_rsi <= 35
                and weekly_trend == "up"
            )
            leader_pullback = (
                regime_status == "risk_on"
                and actual_bb in ("middle", "lower_touch")
                and 35 < actual_rsi <= 55
                and weekly_trend == "up"
            )
            if not (deep_pullback or leader_pullback):
                return json.dumps({
                    "success": False,
                    "message": (
                        f"{ticker} 진입 시나리오 미충족 — "
                        f"regime={regime_status}, bb={actual_bb}, rsi={actual_rsi:.1f}, weekly={weekly_trend}"
                    ),
                    "market_regime": market_regime,
                    "technicals": technicals,
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
                                "profit_loss_amt": 0,
                            })
                        _refresh_sim_portfolio_totals()
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
                        log_trade("BUY", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name, bb_signal=bb_signal, rsi_value=rsi_value)
                else:
                    result = {
                        "success": True,
                        "order_no": "DRY-RUN",
                        "message": f"[시뮬레이션] 매수 {ticker} {qty}주 @ {current_price:,}원 = {total_cost:,}원 (실제 주문 없음)",
                        "reason": reason,
                        "total_cost": total_cost,
                        "dry_run": True,
                    }
                    log_trade("BUY", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name, bb_signal=bb_signal, rsi_value=rsi_value)
            else:
                result = broker.buy_order(ticker, qty)
                result["reason"] = reason
                result["total_cost"] = total_cost
                if result["success"]:
                    log_trade("BUY", ticker, qty, current_price, reason, True, stock_name, bb_signal=bb_signal, rsi_value=rsi_value)

            # 성공 시 일일 매수 횟수 업데이트
            if (result or {}).get("success"):
                _daily_buy_count[ticker] = _daily_buy_count.get(ticker, 0) + 1
                logger.info("일일 매수 횟수: %s → %d/%d회", ticker, _daily_buy_count[ticker], MAX_DAILY_BUY_PER_TICKER)

        elif tool_name == "sell_stock":
            ticker = tool_input["ticker"]
            _validate_ticker(ticker)
            qty = int(tool_input["quantity"])
            reason = tool_input.get("reason", "")
            bb_signal = tool_input.get("bb_signal", "")
            rsi_value = tool_input.get("rsi_value")

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
                    _refresh_sim_portfolio_totals()
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
                log_trade("SELL", ticker, qty, current_price, f"[DRY-RUN] {reason}", False, stock_name, realized_profit, bb_signal=bb_signal, rsi_value=rsi_value)
            else:
                result = broker.sell_order(ticker, qty)
                result["reason"] = reason
                if result["success"]:
                    log_trade("SELL", ticker, qty, current_price, reason, True, stock_name, realized_profit, bb_signal=bb_signal, rsi_value=rsi_value)

            # 손절 매도 시 당일 재진입 블락 등록 (dry-run / 실거래 공통)
            if _is_stoploss_reason(reason) and (result or {}).get("success"):
                _stopped_out_today.add(ticker)
                logger.info("당일 손절 블락 등록: %s", ticker)

        else:
            result = {"error": f"알 수 없는 tool: {tool_name}"}

    except Exception as e:
        result = {"error": str(e)}

    return json.dumps(result, ensure_ascii=False)
