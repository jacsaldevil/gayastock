"""KIS API 기반 재무제표 수집 (DART 불필요)

한국투자증권 API에 재무 엔드포인트가 내장되어 있어
별도의 DART API 키 없이 재무제표 조회가 가능합니다.
"""
from broker.kis import KISBroker

_broker: KISBroker | None = None


def _get_broker() -> KISBroker:
    global _broker
    if _broker is None:
        _broker = KISBroker()
    return _broker


def get_financial_summary(ticker: str, annual: bool = True) -> dict:
    """
    손익계산서 + 대차대조표 + 재무비율을 통합하여 반환.
    가장 최근 기간 기준으로 핵심 지표만 추출합니다.
    """
    broker = _get_broker()

    try:
        income = broker.get_income_statement(ticker, annual=annual)
        balance = broker.get_balance_sheet(ticker, annual=annual)
        ratio = broker.get_financial_ratio(ticker, annual=annual)
    except Exception as e:
        return {"error": f"재무제표 조회 실패: {e}"}

    if not income or not balance:
        return {"error": f"{ticker} 재무데이터 없음"}

    latest_income = income[0]
    latest_balance = balance[0]
    latest_ratio = ratio[0] if ratio else {}

    # 전년 대비 매출 성장률
    yoy_revenue_growth = None
    if len(income) >= 2 and income[1]["revenue"]:
        prev = income[1]["revenue"]
        curr = latest_income["revenue"]
        yoy_revenue_growth = round((curr - prev) / prev * 100, 2) if prev else None

    return {
        "ticker": ticker,
        "period": latest_income["period"],
        # 손익계산서
        "revenue": latest_income["revenue"],
        "operating_profit": latest_income["operating_profit"],
        "net_profit": latest_income["net_profit"],
        "eps": latest_income["eps"],
        # 대차대조표
        "total_assets": latest_balance["total_assets"],
        "total_equity": latest_balance["total_equity"],
        "total_debt": latest_balance["total_debt"],
        "debt_ratio_pct": latest_balance["debt_ratio_pct"],
        # 재무비율
        "roe_pct": latest_ratio.get("roe_pct", 0),
        "operating_margin_pct": latest_ratio.get("operating_margin_pct", 0),
        "net_margin_pct": latest_ratio.get("net_margin_pct", 0),
        "per": latest_ratio.get("per", 0),
        "pbr": latest_ratio.get("pbr", 0),
        # 성장성
        "yoy_revenue_growth_pct": yoy_revenue_growth,
        # 원시 데이터 (Claude가 추가 분석 가능하도록)
        "income_history": income[:3],
        "balance_history": balance[:3],
    }
