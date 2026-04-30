"""한국투자증권 Open API 래퍼 (모의투자/실투자 공통)"""
import json
import time
import requests
from datetime import datetime, timedelta
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_BASE_URL, KIS_MOCK


class KISBroker:
    def __init__(self):
        self.base_url = KIS_BASE_URL
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        self.account_no = KIS_ACCOUNT_NO  # "01234567-01" 형식
        self._access_token = None
        self._token_expires_at = None

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
        return self._access_token

    def _headers(self, tr_id: str, extra: dict = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        if KIS_MOCK:
            h["custtype"] = "P"
        if extra:
            h.update(extra)
        return h

    def get_current_price(self, ticker: str) -> dict:
        """주식 현재가 조회 (FHKST01010100)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        res = requests.get(url, headers=self._headers("FHKST01010100"), params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output", {})
        return {
            "ticker": ticker,
            "current_price": int(output.get("stck_prpr", 0)),
            "open_price": int(output.get("stck_oprc", 0)),
            "high_price": int(output.get("stck_hgpr", 0)),
            "low_price": int(output.get("stck_lwpr", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "change_rate": float(output.get("prdy_ctrt", 0)),
            "per": float(output.get("per", 0) or 0),
            "pbr": float(output.get("pbr", 0) or 0),
            "eps": float(output.get("eps", 0) or 0),
        }

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

        holdings = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty > 0:
                holdings.append({
                    "ticker": item.get("pdno"),
                    "name": item.get("prdt_name"),
                    "quantity": qty,
                    "avg_price": float(item.get("pchs_avg_pric", 0)),
                    "current_price": int(item.get("prpr", 0)),
                    "profit_loss_rate": float(item.get("evlu_pfls_rt", 0)),
                })

        summary = data.get("output2", [{}])[0]
        return {
            "cash": int(summary.get("dnca_tot_amt", 0)),
            "total_eval": int(summary.get("tot_evlu_amt", 0)),
            "profit_loss": int(summary.get("evlu_pfls_smtl_amt", 0)),
            "holdings": holdings,
        }

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
        return {
            "success": data.get("rt_cd") == "0",
            "order_no": data.get("output", {}).get("odno", ""),
            "message": data.get("msg1", ""),
        }
