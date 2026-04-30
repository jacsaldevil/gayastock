"""한국투자증권 Open API 래퍼 (모의투자/실투자 공통)

공식 레퍼런스: https://github.com/koreainvestment/open-trading-api
"""
import json
import os
import time
import requests
from datetime import datetime, timedelta
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_BASE_URL, KIS_MOCK

TOKEN_CACHE_FILE = ".kis_token_cache.json"


class KISBroker:
    def __init__(self):
        self.base_url = KIS_BASE_URL
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        self.account_no = KIS_ACCOUNT_NO  # "01234567-01" 형식
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._load_cached_token()

    # ── 인증 ─────────────────────────────────────────────

    def _load_cached_token(self):
        """파일에 저장된 토큰 재사용 (공식 패턴)"""
        if not os.path.exists(TOKEN_CACHE_FILE):
            return
        try:
            with open(TOKEN_CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data["expires_at"])
            if datetime.now() < expires_at:
                self._access_token = data["access_token"]
                self._token_expires_at = expires_at
        except Exception:
            pass

    def _save_token_cache(self, token: str, expires_at: datetime):
        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expires_at": expires_at.isoformat()}, f)

    def _get_token(self) -> str:
        if self._access_token and self._token_expires_at and datetime.now() < self._token_expires_at:
            return self._access_token

        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        res = requests.post(url, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._access_token = data["access_token"]
        self._token_expires_at = datetime.now() + timedelta(hours=23)
        self._save_token_cache(self._access_token, self._token_expires_at)
        return self._access_token

    def _headers(self, tr_id: str, extra: dict = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "Authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if extra:
            h.update(extra)
        return h

    def _smart_sleep(self):
        """초당 API 호출 제한 방지 (공식 패턴 참고)"""
        time.sleep(0.05)

    # ── 시세 조회 ──────────────────────────────────────────

    def get_current_price(self, ticker: str) -> dict:
        """주식 현재가 + PER/PBR/EPS 조회 (TR: FHKST01010100)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        res = requests.get(url, headers=self._headers("FHKST01010100"), params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output", {})
        self._smart_sleep()
        return {
            "ticker": ticker,
            "current_price": int(output.get("stck_prpr", 0) or 0),
            "open_price": int(output.get("stck_oprc", 0) or 0),
            "high_price": int(output.get("stck_hgpr", 0) or 0),
            "low_price": int(output.get("stck_lwpr", 0) or 0),
            "volume": int(output.get("acml_vol", 0) or 0),
            "change_rate": float(output.get("prdy_ctrt", 0) or 0),
            "per": float(output.get("per", 0) or 0),
            "pbr": float(output.get("pbr", 0) or 0),
            "eps": float(output.get("eps", 0) or 0),
            "bps": float(output.get("bps", 0) or 0),
        }

    # ── 재무제표 (KIS 자체 API, DART 불필요) ──────────────

    def get_income_statement(self, ticker: str, annual: bool = True) -> list[dict]:
        """손익계산서 (TR: FHKST66430200) — 최대 4개 연도/분기"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/finance/income-statement"
        params = {
            "FID_DIV_CLS_CODE": "0" if annual else "1",  # 0=연간, 1=분기
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        res = requests.get(url, headers=self._headers("FHKST66430200"), params=params, timeout=10)
        res.raise_for_status()
        self._smart_sleep()
        rows = res.json().get("output", [])
        result = []
        for r in rows:
            result.append({
                "period": r.get("stac_yymm", ""),        # 결산년월
                "revenue": _to_int(r.get("sale_account")),       # 매출액
                "operating_profit": _to_int(r.get("sale_totl_prfi")),  # 영업이익
                "net_profit": _to_int(r.get("bsop_prti")),       # 당기순이익
                "eps": _to_float(r.get("eps")),
            })
        return result

    def get_balance_sheet(self, ticker: str, annual: bool = True) -> list[dict]:
        """대차대조표 (TR: FHKST66430100)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/finance/balance-sheet"
        params = {
            "FID_DIV_CLS_CODE": "0" if annual else "1",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        res = requests.get(url, headers=self._headers("FHKST66430100"), params=params, timeout=10)
        res.raise_for_status()
        self._smart_sleep()
        rows = res.json().get("output", [])
        result = []
        for r in rows:
            total_equity = _to_int(r.get("total_cptl"))
            total_debt = _to_int(r.get("total_libl"))
            debt_ratio = round(total_debt / total_equity * 100, 2) if total_equity else 0
            result.append({
                "period": r.get("stac_yymm", ""),
                "total_assets": _to_int(r.get("total_aset")),    # 자산총계
                "total_equity": total_equity,                     # 자본총계
                "total_debt": total_debt,                         # 부채총계
                "debt_ratio_pct": debt_ratio,
            })
        return result

    def get_financial_ratio(self, ticker: str, annual: bool = True) -> list[dict]:
        """재무비율 (TR: FHKST66430300) — ROE, 영업이익률 등"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/finance/financial-ratio"
        params = {
            "FID_DIV_CLS_CODE": "0" if annual else "1",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        res = requests.get(url, headers=self._headers("FHKST66430300"), params=params, timeout=10)
        res.raise_for_status()
        self._smart_sleep()
        rows = res.json().get("output", [])
        result = []
        for r in rows:
            result.append({
                "period": r.get("stac_yymm", ""),
                "roe_pct": _to_float(r.get("roe_val")),            # ROE
                "operating_margin_pct": _to_float(r.get("bsop_prfi_rate")),  # 영업이익률
                "net_margin_pct": _to_float(r.get("net_prfi_rate")),          # 순이익률
                "per": _to_float(r.get("per")),
                "pbr": _to_float(r.get("pbr")),
            })
        return result

    # ── 잔고 조회 ─────────────────────────────────────────

    def get_balance(self) -> dict:
        """잔고 및 보유 종목 조회 (VTTC8434R 모의 / TTTC8434R 실투자)"""
        tr_id = "VTTC8434R" if KIS_MOCK else "TTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        acc_no, acc_suffix = self.account_no.split("-")
        params = {
            "CANO": acc_no,
            "ACNT_PRDT_CD": acc_suffix,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        res = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._smart_sleep()

        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0) or 0)
            if qty > 0:
                holdings.append({
                    "ticker": item.get("pdno"),
                    "name": item.get("prdt_name"),
                    "quantity": qty,
                    "avg_price": _to_float(item.get("pchs_avg_pric")),
                    "current_price": int(item.get("prpr", 0) or 0),
                    "profit_loss_rate": _to_float(item.get("evlu_pfls_rt")),
                })

        summary = data.get("output2", [{}])[0]
        return {
            "cash": int(summary.get("dnca_tot_amt", 0) or 0),
            "total_eval": int(summary.get("tot_evlu_amt", 0) or 0),
            "profit_loss": int(summary.get("evlu_pfls_smtl_amt", 0) or 0),
            "holdings": holdings,
        }

    # ── 주문 ──────────────────────────────────────────────

    def buy_order(self, ticker: str, quantity: int, price: int = 0) -> dict:
        """매수 주문 (price=0 이면 시장가)"""
        tr_id = "VTTC0802U" if KIS_MOCK else "TTTC0802U"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        acc_no, acc_suffix = self.account_no.split("-")
        body = {
            "CANO": acc_no,
            "ACNT_PRDT_CD": acc_suffix,
            "PDNO": ticker,
            "ORD_DVSN": "01" if price == 0 else "00",  # 01=시장가, 00=지정가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }
        res = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._smart_sleep()
        return {
            "success": data.get("rt_cd") == "0",
            "order_no": data.get("output", {}).get("odno", ""),
            "message": data.get("msg1", ""),
        }

    def sell_order(self, ticker: str, quantity: int, price: int = 0) -> dict:
        """매도 주문 (price=0 이면 시장가)"""
        tr_id = "VTTC0801U" if KIS_MOCK else "TTTC0801U"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        acc_no, acc_suffix = self.account_no.split("-")
        body = {
            "CANO": acc_no,
            "ACNT_PRDT_CD": acc_suffix,
            "PDNO": ticker,
            "ORD_DVSN": "01" if price == 0 else "00",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }
        res = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._smart_sleep()
        return {
            "success": data.get("rt_cd") == "0",
            "order_no": data.get("output", {}).get("odno", ""),
            "message": data.get("msg1", ""),
        }


def _to_int(val) -> int:
    try:
        return int(str(val).replace(",", "")) if val else 0
    except (ValueError, TypeError):
        return 0


def _to_float(val) -> float:
    try:
        return float(str(val).replace(",", "")) if val else 0.0
    except (ValueError, TypeError):
        return 0.0
