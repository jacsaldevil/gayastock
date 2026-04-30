"""DART(금융감독원 전자공시) API로 재무제표 데이터 수집"""
import requests
from config import DART_API_KEY, DART_BASE_URL


def _get_corp_code(ticker: str) -> str | None:
    """종목코드 → DART 고유번호 변환"""
    url = f"{DART_BASE_URL}/company.json"
    res = requests.get(url, params={"crtfc_key": DART_API_KEY, "stock_code": ticker}, timeout=10)
    res.raise_for_status()
    data = res.json()
    if data.get("status") == "000":
        return data.get("corp_code")
    return None


def get_financial_summary(ticker: str, year: str = None) -> dict:
    """
    최근 연간 재무 요약 반환.
    year: "2023" 형식, None이면 가장 최근 사업연도 자동 탐색
    """
    from datetime import datetime
    if year is None:
        year = str(datetime.now().year - 1)  # 전년도

    corp_code = _get_corp_code(ticker)
    if not corp_code:
        return {"error": f"DART에서 {ticker} 종목을 찾을 수 없습니다."}

    url = f"{DART_BASE_URL}/fnlttSinglAcntAll.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bsns_year": year,
        "reprt_code": "11011",  # 사업보고서
        "fs_div": "CFS",        # 연결재무제표 (없으면 OFS 개별 시도)
    }
    res = requests.get(url, params=params, timeout=15)
    res.raise_for_status()
    data = res.json()

    if data.get("status") != "000":
        # 연결재무제표 없으면 개별재무제표 시도
        params["fs_div"] = "OFS"
        res = requests.get(url, params=params, timeout=15)
        res.raise_for_status()
        data = res.json()

    if data.get("status") != "000":
        return {"error": f"재무제표 조회 실패: {data.get('message', '')}"}

    items = data.get("list", [])

    def find(account_name: str) -> float:
        for item in items:
            if account_name in item.get("account_nm", ""):
                val = item.get("thstrm_amount", "0").replace(",", "").replace("-", "0")
                try:
                    return float(val)
                except ValueError:
                    return 0.0
        return 0.0

    revenue = find("매출액")
    operating_profit = find("영업이익")
    net_profit = find("당기순이익")
    total_assets = find("자산총계")
    total_equity = find("자본총계")
    total_debt = find("부채총계")

    roe = (net_profit / total_equity * 100) if total_equity else 0
    debt_ratio = (total_debt / total_equity * 100) if total_equity else 0
    operating_margin = (operating_profit / revenue * 100) if revenue else 0

    return {
        "ticker": ticker,
        "year": year,
        "revenue": revenue,
        "operating_profit": operating_profit,
        "net_profit": net_profit,
        "total_assets": total_assets,
        "total_equity": total_equity,
        "total_debt": total_debt,
        "roe_pct": round(roe, 2),
        "debt_ratio_pct": round(debt_ratio, 2),
        "operating_margin_pct": round(operating_margin, 2),
    }
