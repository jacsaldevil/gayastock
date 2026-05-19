"""한국투자증권 Open API 래퍼 (모의투자/실투자 공통)

공식 레퍼런스: https://github.com/koreainvestment/open-trading-api
"""
import json
import logging
import os
import time
import requests
from datetime import datetime, timedelta
from data.utils import get_now_kst
from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_BASE_URL, KIS_MOCK

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = ".kis_token_cache.json"
TOKEN_CACHE_BLOB = "kis_token_cache.json"
# GCS_TOKEN_BUCKET 우선, 없으면 GCS_DATA_BUCKET 공유 (Cloud Run 환경)
_GCS_BUCKET = os.environ.get("GCS_TOKEN_BUCKET") or os.environ.get("GCS_DATA_BUCKET", "")


def _gcs_read_token() -> dict | None:
    if not _GCS_BUCKET:
        return None
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(_GCS_BUCKET).blob(TOKEN_CACHE_BLOB)
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as e:
        logger.debug("GCS 토큰 읽기 실패: %s", e)
    return None


def _gcs_write_token(data: dict):
    if not _GCS_BUCKET:
        return
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(_GCS_BUCKET).blob(TOKEN_CACHE_BLOB)
        blob.upload_from_string(json.dumps(data), content_type="application/json")
    except Exception as e:
        logger.debug("GCS 토큰 저장 실패: %s", e)


class KISBroker:
    def __init__(self):
        self.base_url = KIS_BASE_URL
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        raw = KIS_ACCOUNT_NO.strip()
        # "50123456-01" 또는 "5012345601" 두 형식 모두 허용
        if "-" in raw:
            self.acc_no, self.acc_suffix = raw.split("-", 1)
        else:
            self.acc_no, self.acc_suffix = raw[:8], raw[8:]
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._load_cached_token()

    # ── 인증 ─────────────────────────────────────────────

    def _load_cached_token(self):
        """GCS → 로컬 파일 순서로 캐시된 토큰 재사용"""
        # 1. GCS 우선 시도
        data = _gcs_read_token()
        # 2. GCS 없으면 로컬 파일
        if data is None and os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return
        if data is None:
            return
        try:
            expires_at = datetime.fromisoformat(data["expires_at"])
            if get_now_kst() < expires_at:
                self._access_token = data["access_token"]
                self._token_expires_at = expires_at
        except Exception:
            pass

    def _save_token_cache(self, token: str, expires_at: datetime):
        data = {"access_token": token, "expires_at": expires_at.isoformat()}
        # GCS와 로컬 둘 다 저장 (로컬은 개발 환경 fallback)
        _gcs_write_token(data)
        try:
            with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(TOKEN_CACHE_FILE, 0o600)
        except Exception:
            pass

    def _get_token(self) -> str:
        if self._access_token and self._token_expires_at and get_now_kst() < self._token_expires_at:
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
        if "access_token" not in data:
            raise ValueError(f"KIS 토큰 발급 실패: {data.get('msg1', data.get('msg', str(data)))}")
        self._access_token = data["access_token"]
        self._token_expires_at = get_now_kst() + timedelta(hours=23)
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

    def get_top_volume_stocks(self, n: int = 20) -> list[dict]:
        """거래량 상위 순수 주식 종목 조회 (ETF/ETN 제외, TR: FHPST01710000)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "111111",  # 거래정지/관리/우선주/투자유의/ETF/스팩 제외
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        # 충분히 많이 가져와서 필터 후 n개 확보
        res = requests.get(url, headers=self._headers("FHPST01710000"), params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output", [])
        self._smart_sleep()
        result = []
        for item in output:
            ticker = item.get("mksc_shrn_iscd", "")
            name = item.get("hts_kor_isnm", "")
            if not ticker or _is_fund(name):
                continue
            result.append({
                "ticker": ticker,
                "name": name,
                "current_price": _to_int(item.get("stck_prpr")),
                "volume": _to_int(item.get("acml_vol")),
                "change_rate": _to_float(item.get("prdy_ctrt")),
            })
            if len(result) >= n:
                break
        return result

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
        current_price = int(output.get("stck_prpr", 0) or 0)
        if current_price == 0:
            raise ValueError(f"{ticker} 현재가 조회 실패 (거래정지 또는 API 오류)")
        return {
            "ticker": ticker,
            "name": output.get("hts_kor_isnm", ""),
            "current_price": current_price,
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

    def _get_finance(self, url: str, tr_id: str, params: dict) -> dict:
        """재무 API 호출 — 500 에러 시 1회 재시도"""
        for attempt in range(2):
            try:
                res = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
                if res.status_code == 500 and attempt == 0:
                    logger.warning("재무 API 500 에러, 1초 후 재시도 (%s)", url)
                    time.sleep(1)
                    continue
                res.raise_for_status()
                self._smart_sleep()
                return res.json()
            except requests.HTTPError as e:
                if attempt == 1:
                    raise
                logger.warning("재무 API 오류 재시도: %s", e)
                time.sleep(1)
        return {}

    def get_income_statement(self, ticker: str, annual: bool = True) -> list[dict]:
        """손익계산서 (TR: FHKST66430200) — 최대 4개 연도/분기"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/finance/income-statement"
        params = {
            "FID_DIV_CLS_CODE": "0" if annual else "1",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        data = self._get_finance(url, "FHKST66430200", params)
        rows = data.get("output", [])
        result = []
        for r in rows:
            revenue = _to_int(r.get("sale_account"))
            operating_profit = _to_int(r.get("bsop_prti"))        # 영업이익
            net_profit = _to_int(r.get("thtr_ntin"))               # 당기순이익
            result.append({
                "period": r.get("stac_yymm", ""),
                "revenue": revenue,
                "operating_profit": operating_profit,
                "net_profit": net_profit,
                "eps": _to_float(r.get("eps")),
                # 영업이익률 직접 계산 (ratio API 0 반환 대비 fallback)
                "operating_margin_pct": round(operating_profit / revenue * 100, 2) if revenue else 0,
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
        data = self._get_finance(url, "FHKST66430100", params)
        rows = data.get("output", [])
        result = []
        for r in rows:
            total_equity = _to_int(r.get("total_cptl"))
            total_debt = _to_int(r.get("total_libl"))
            debt_ratio = round(total_debt / total_equity * 100, 2) if total_equity else 0
            result.append({
                "period": r.get("stac_yymm", ""),
                "total_assets": _to_int(r.get("total_aset")),
                "total_equity": total_equity,
                "total_debt": total_debt,
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
        data = self._get_finance(url, "FHKST66430300", params)
        rows = data.get("output", [])
        result = []
        for r in rows:
            result.append({
                "period": r.get("stac_yymm", ""),
                "roe_pct": _to_float(r.get("roe_val")),
                "operating_margin_pct": _to_float(r.get("bsop_prfi_rate")),
                "net_margin_pct": _to_float(r.get("net_prfi_rate")),
                "per": _to_float(r.get("per")),
                "pbr": _to_float(r.get("pbr")),
            })
        return result

    # ── 잔고 조회 ─────────────────────────────────────────

    def get_balance(self) -> dict:
        """잔고 및 보유 종목 조회 (VTTC8434R 모의 / TTTC8434R 실투자)"""
        tr_id = "VTTC8434R" if KIS_MOCK else "TTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.acc_no,
            "ACNT_PRDT_CD": self.acc_suffix,
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
            if qty <= 0:
                continue

            # 평균단가: pchs_avg_pric 우선, 0이면 pchs_amt(매입금액) / qty로 역산
            avg = _to_float(item.get("pchs_avg_pric"))
            if avg == 0:
                pchs_amt = _to_float(item.get("pchs_amt", 0))
                avg = round(pchs_amt / qty, 2) if pchs_amt > 0 else 0

            cur = int(item.get("prpr", 0) or 0)

            # 수익률: avg가 확보된 경우 직접 계산, 아니면 API evlu_pfls_rt 사용
            if avg > 0:
                pl_rate = round((cur - avg) / avg * 100, 2)
            else:
                pl_rate = _to_float(item.get("evlu_pfls_rt"))

            # 손익금액: evlu_pfls_amt 직접 사용, 없으면 계산
            pl_amt = _to_int(item.get("evlu_pfls_amt", 0))
            if pl_amt == 0 and avg > 0:
                pl_amt = int((cur - avg) * qty)

            holdings.append({
                "ticker": item.get("pdno"),
                "name": item.get("prdt_name"),
                "quantity": qty,
                "avg_price": avg,
                "current_price": cur,
                "profit_loss_rate": pl_rate,
                "profit_loss_amt": pl_amt,
            })

        summary = data.get("output2", [{}])[0]
        cash = int(summary.get("dnca_tot_amt", 0) or 0)
        total_eval = int(summary.get("tot_evlu_amt", 0) or 0)

        # 손익 계산 우선순위:
        # 1) evlu_pfls_smtl_amt (API 합계)
        # 2) scts_evlu_amt - pchs_amt_smtl_amt (보유주식 평가 - 매입금액 합계)
        # 3) holdings 개별 합산
        # 4) total_eval - cash - 매입금액 (예수금 방식)
        api_pl = int(summary.get("evlu_pfls_smtl_amt", 0) or 0)
        scts_evlu = int(summary.get("scts_evlu_amt", 0) or 0)
        pchs_smtl = int(summary.get("pchs_amt_smtl_amt", 0) or 0)

        if api_pl != 0:
            profit_loss = api_pl
        elif pchs_smtl > 0:
            profit_loss = scts_evlu - pchs_smtl
        else:
            holdings_pl = sum(h["profit_loss_amt"] for h in holdings)
            profit_loss = holdings_pl if holdings_pl != 0 else (total_eval - cash) - pchs_smtl

        return {
            "cash": cash,             # dnca_tot_amt: 예수금 (주식 매수 가능한 현금)
            "holdings_eval": scts_evlu,  # scts_evlu_amt: 보유 주식 평가금액
            "total_eval": total_eval,    # tot_evlu_amt: KIS 총평가 (참고용)
            "profit_loss": profit_loss,
            "holdings": holdings,
        }

    # ── 주문 ──────────────────────────────────────────────

    def buy_order(self, ticker: str, quantity: int, price: int = 0) -> dict:
        """매수 주문 (price=0 이면 시장가)"""
        tr_id = "VTTC0802U" if KIS_MOCK else "TTTC0802U"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO": self.acc_no,
            "ACNT_PRDT_CD": self.acc_suffix,
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
        body = {
            "CANO": self.acc_no,
            "ACNT_PRDT_CD": self.acc_suffix,
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

    # ── 분봉 / 하이킨아시 ────────────────────────────────────

    def get_minute_candles(self, ticker: str, ha_candle_count: int = 30) -> dict:
        """1분봉 조회 후 VWAP 계산 + 3분봉 집계 + 하이킨아시 계산 반환 (TR: FHKST03010200)"""
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        now = get_now_kst().strftime("%H%M%S")
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_HOUR_1": now,
            "FID_PW_DATA_INCU_YN": "Y",
        }
        res = requests.get(url, headers=self._headers("FHKST03010200"), params=params, timeout=10)
        res.raise_for_status()
        data_json = res.json()
        output = data_json.get("output2", [])
        stock_name = data_json.get("output1", {}).get("hts_kor_isnm", "")
        self._smart_sleep()

        # 오래된 순으로 정렬 (API는 최신 순 반환), 전체 데이터 수집
        all_candles_1m = []
        for r in reversed(output):
            all_candles_1m.append({
                "time": r.get("stck_cntg_hour", ""),
                "open": _to_int(r.get("stck_oprc")),
                "high": _to_int(r.get("stck_hgpr")),
                "low": _to_int(r.get("stck_lwpr")),
                "close": _to_int(r.get("stck_prpr")),
                "volume": _to_int(r.get("cntg_vol")),
            })

        current_price = all_candles_1m[-1]["close"] if all_candles_1m else 0
        vwap_data = _compute_vwap(all_candles_1m, current_price)

        # HA: 최근 ha_candle_count개 1분봉 → 3분봉 집계 → HA 계산
        recent_1m = all_candles_1m[-ha_candle_count:] if len(all_candles_1m) > ha_candle_count else all_candles_1m
        candles_3m = _aggregate_3min(recent_1m)
        ha_candles = _compute_heikin_ashi(candles_3m)

        return {
            "candles": ha_candles,
            "vwap": vwap_data["vwap"],
            "vwap_deviation_pct": vwap_data["deviation_pct"],
            "current_price": current_price,
            "name": stock_name,
        }

    # ── 체결 이력 ─────────────────────────────────────────

    def get_pending_orders(self) -> list[dict]:
        """미체결 주문 조회 (TTTC8036R 실전 / VTTC8036R 모의)"""
        tr_id = "VTTC8036R" if KIS_MOCK else "TTTC8036R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        params = {
            "CANO": self.acc_no,
            "ACNT_PRDT_CD": self.acc_suffix,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
        }
        res = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output", [])
        self._smart_sleep()
        result = []
        for item in output:
            qty = int(item.get("ord_qty", 0) or 0)
            filled = int(item.get("tot_ccld_qty", 0) or 0)
            remaining = qty - filled
            if remaining <= 0:
                continue
            result.append({
                "order_no": item.get("odno", ""),
                "krx_fwdg_ord_orgno": item.get("krx_fwdg_ord_orgno", ""),
                "ticker": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "action": "BUY" if item.get("sll_buy_dvsn_cd") == "02" else "SELL",
                "order_qty": qty,
                "filled_qty": filled,
                "remaining_qty": remaining,
                "order_price": _to_int(item.get("ord_unpr")),
                "order_type": item.get("ord_dvsn_name", ""),
            })
        return result

    def cancel_order(self, order_no: str, krx_fwdg_ord_orgno: str = "") -> dict:
        """주문 취소 (TTTC0803U 실전 / VTTC0803U 모의) — 전량 취소"""
        tr_id = "VTTC0803U" if KIS_MOCK else "TTTC0803U"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        body = {
            "CANO": self.acc_no,
            "ACNT_PRDT_CD": self.acc_suffix,
            "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno,
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "02",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        res = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._smart_sleep()
        return {
            "success": data.get("rt_cd") == "0",
            "order_no": order_no,
            "message": data.get("msg1", ""),
        }

    def cancel_all_pending_orders(self) -> list[dict]:
        """미체결 주문 전량 취소 후 취소 결과 목록 반환"""
        pending = self.get_pending_orders()
        results = []
        for order in pending:
            try:
                r = self.cancel_order(order["order_no"], order.get("krx_fwdg_ord_orgno", ""))
                r["ticker"] = order["ticker"]
                r["name"] = order["name"]
                results.append(r)
                logger.info("주문 취소: %s %s → %s", order["ticker"], order["order_no"], r["message"])
            except Exception as e:
                logger.error("주문 취소 실패: %s %s → %s", order["ticker"], order["order_no"], e)
                results.append({
                    "success": False,
                    "order_no": order["order_no"],
                    "ticker": order["ticker"],
                    "name": order["name"],
                    "message": str(e),
                })
        return results

    def get_order_history(self, start_date: str, end_date: str) -> list[dict]:
        """일별 주문체결 조회 (TTTC8001R 실전 / VTTC8001R 모의)
        start_date / end_date: 'YYYYMMDD' 형식
        """
        tr_id = "VTTC8001R" if KIS_MOCK else "TTTC8001R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        params = {
            "CANO": self.acc_no,
            "ACNT_PRDT_CD": self.acc_suffix,
            "INQR_STRT_DT": start_date,
            "INQR_END_DT": end_date,
            "SLL_BUY_DVSN_CD": "00",   # 00=전체
            "INQR_DVSN": "00",          # 00=역순(최신순)
            "PDNO": "",
            "CCLD_DVSN": "01",          # 01=체결만
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        res = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._smart_sleep()

        result = []
        for item in data.get("output1", []):
            qty = _to_int(item.get("tot_ccld_qty"))
            if qty == 0:
                continue
            action = "SELL" if item.get("sll_buy_dvsn_cd") == "01" else "BUY"
            price = _to_int(item.get("avg_prvs"))
            result.append({
                "ts": item.get("ord_dt", "") + " " + item.get("ord_tmd", ""),
                "action": action,
                "ticker": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "quantity": qty,
                "price": price,
                "amount": price * qty,
                "order_no": item.get("odno", ""),
                "order_state": item.get("ord_psbl_yn", ""),
            })
        return result


# ETF/ETN/펀드 제외용 이름 키워드
_FUND_KEYWORDS = (
    "KODEX", "TIGER", "ARIRANG", "KINDEX", "HANARO", "KOSEF",
    "KBSTAR", "ACE", "SOL", "WOORI", "SMART", "FOCUS", "TIMEFOLIO",
    "ETF", "ETN", "레버리지", "인버스", "선물", "리츠", "REIT",
)

def _is_fund(name: str) -> bool:
    upper = name.upper()
    return any(kw.upper() in upper for kw in _FUND_KEYWORDS)


def _compute_vwap(candles_1m: list[dict], current_price: int) -> dict:
    """1분봉 전체로 VWAP 계산. deviation_pct = (현재가 - VWAP) / VWAP × 100"""
    total_tp_vol = 0.0
    total_vol = 0
    for c in candles_1m:
        if c["volume"] == 0:
            continue
        tp = (c["high"] + c["low"] + c["close"]) / 3
        total_tp_vol += tp * c["volume"]
        total_vol += c["volume"]

    if total_vol == 0 or current_price == 0:
        return {"vwap": 0, "deviation_pct": 0.0}

    vwap = round(total_tp_vol / total_vol)
    deviation_pct = round((current_price - vwap) / vwap * 100, 2)
    return {"vwap": vwap, "deviation_pct": deviation_pct}


def _aggregate_3min(candles_1m: list[dict]) -> list[dict]:
    """1분봉 리스트를 3분봉으로 집계"""
    result = []
    n = len(candles_1m)
    trimmed = candles_1m[n % 3:]  # 앞 자투리 제거, 3의 배수로 맞춤
    for i in range(0, len(trimmed), 3):
        group = trimmed[i:i + 3]
        if not group:
            continue
        result.append({
            "time": group[0]["time"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "volume": sum(c["volume"] for c in group),
        })
    return result


def _compute_heikin_ashi(candles: list[dict]) -> list[dict]:
    """OHLC 캔들 리스트로 하이킨아시 계산"""
    ha = []
    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        if i == 0:
            ha_open = (c["open"] + c["close"]) / 2
        else:
            ha_open = (ha[i - 1]["ha_open"] + ha[i - 1]["ha_close"]) / 2
        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)
        body = ha_close - ha_open
        upper_wick = ha_high - max(ha_open, ha_close)
        lower_wick = min(ha_open, ha_close) - ha_low
        bullish = body > 0

        if bullish:
            pattern = "강한상승" if upper_wick < abs(body) * 0.15 else "상승저항(윗꼬리)"
        else:
            pattern = "강한하락" if lower_wick < abs(body) * 0.15 else "하락저지(아랫꼬리)"

        ha.append({
            "time": c["time"],
            "ha_open": round(ha_open),
            "ha_high": round(ha_high),
            "ha_low": round(ha_low),
            "ha_close": round(ha_close),
            "volume": c["volume"],
            "bullish": bullish,
            "upper_wick": round(upper_wick),
            "lower_wick": round(lower_wick),
            "pattern": pattern,
        })
    return ha


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
