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
from config import (
    KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_BASE_URL, KIS_MOCK,
    MARKET_PROXY_TICKER, MARKET_CRASH_PCT,
)

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = ".kis_token_cache.json"
TOKEN_CACHE_BLOB = "kis_token_cache.json"
_TOKEN_EXPIRY_BUFFER = timedelta(minutes=10)  # 만료 10분 전에 미리 갱신


def _token_gcs_bucket() -> str:
    """토큰 전용 비공개 버킷(GCS_TOKEN_BUCKET) 우선, 없으면 데이터 버킷으로 폴백."""
    token_bucket = os.environ.get("GCS_TOKEN_BUCKET", "").strip()
    if token_bucket:
        return token_bucket
    data_bucket = os.environ.get("GCS_DATA_BUCKET", "").strip()
    if not data_bucket:
        logger.warning("GCS_TOKEN_BUCKET / GCS_DATA_BUCKET 미설정 — KIS 토큰 캐시 불가 (매 실행 신규 발급)")
    return data_bucket


def _gcs_read_token() -> dict | None:
    bucket = _token_gcs_bucket()
    if not bucket:
        logger.warning("GCS_DATA_BUCKET 미설정 — KIS 토큰 GCS 캐시 불가 (매 실행마다 신규 발급)")
        return None
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(bucket).blob(TOKEN_CACHE_BLOB)
        text = blob.download_as_text()
        data = json.loads(text)
        logger.debug("GCS 토큰 캐시 읽기 성공 (bucket=%s)", bucket)
        return data
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "NotFound" in type(e).__name__:
            logger.info("GCS 토큰 캐시 없음 (첫 실행) — 신규 토큰 발급")
        else:
            logger.warning("GCS 토큰 읽기 실패 [%s] %s — 신규 토큰 발급", type(e).__name__, e)
    return None


def _gcs_write_token(data: dict):
    bucket = _token_gcs_bucket()
    if not bucket:
        return
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(bucket).blob(TOKEN_CACHE_BLOB)
        blob.upload_from_string(json.dumps(data), content_type="application/json")
        logger.debug("GCS 토큰 캐시 저장 완료 (bucket=%s)", bucket)
    except Exception as e:
        logger.warning("GCS 토큰 저장 실패 [%s] %s", type(e).__name__, e)


_KIS_TOKEN_ERROR_CODES = frozenset({"EGW00121", "EGW00123", "EGW00124"})


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
        """GCS → 로컬 파일 순서로 캐시된 토큰 로드. 만료 10분 이내면 미사용."""
        from data.utils import KST
        data = _gcs_read_token()
        if data is None and os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                logger.debug("로컬 토큰 캐시 파일 읽기 성공")
            except Exception:
                pass
        if data is None:
            return
        try:
            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=KST)
            now = get_now_kst()
            remaining_sec = (expires_at - now).total_seconds()
            if remaining_sec > _TOKEN_EXPIRY_BUFFER.total_seconds():
                self._access_token = data["access_token"]
                self._token_expires_at = expires_at
                logger.info("KIS 토큰 캐시 재사용 — 잔여 %d분 (만료: %s)",
                            int(remaining_sec / 60), expires_at.strftime("%m-%d %H:%M"))
            else:
                logger.info("KIS 토큰 캐시 만료 임박(잔여 %.0f분) 또는 만료됨 — 신규 발급",
                            max(0, remaining_sec / 60))
        except Exception as e:
            logger.warning("KIS 토큰 캐시 파싱 실패 [%s] %s — 신규 발급", type(e).__name__, e)

    def _save_token_cache(self, token: str, expires_at: datetime):
        data = {"access_token": token, "expires_at": expires_at.isoformat()}
        _gcs_write_token(data)
        try:
            with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(TOKEN_CACHE_FILE, 0o600)
        except Exception:
            pass

    def _get_token(self) -> str:
        if self._access_token and self._token_expires_at:
            remaining_sec = (self._token_expires_at - get_now_kst()).total_seconds()
            if remaining_sec > _TOKEN_EXPIRY_BUFFER.total_seconds():
                return self._access_token
            logger.info("KIS 토큰 만료 임박 (잔여 %.0f분) — 갱신", max(0, remaining_sec / 60))

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
        raw_exp = data.get("access_token_token_expired", "")
        try:
            from data.utils import KST
            self._token_expires_at = datetime.strptime(raw_exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except Exception:
            self._token_expires_at = get_now_kst() + timedelta(hours=23)
        logger.info("KIS 신규 토큰 발급 완료 (만료: %s)", self._token_expires_at.strftime("%Y-%m-%d %H:%M"))
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

    def _api_call(self, method: str, url: str, tr_id: str, **kwargs) -> requests.Response:
        """KIS API 단일 호출 포인트 — 토큰 오류 1회 재발급, 500 에러 최대 3회 재시도"""
        _token_reissued = False
        for attempt in range(4):
            res = requests.request(method, url, headers=self._headers(tr_id), **kwargs)
            # 토큰 오류 — 1회만 재발급 후 재시도
            if not _token_reissued:
                need_reissue = res.status_code == 401
                msg_cd = ""
                if not need_reissue:
                    try:
                        body = res.json()
                        msg_cd = body.get("msg_cd", "")
                        if body.get("rt_cd") == "1" and msg_cd in _KIS_TOKEN_ERROR_CODES:
                            need_reissue = True
                    except Exception:
                        pass
                if need_reissue:
                    logger.warning("KIS 토큰 거부 감지 (HTTP %d, msg_cd=%s) — 재발급 후 재시도",
                                   res.status_code, msg_cd)
                    self._access_token = None
                    self._token_expires_at = None
                    _token_reissued = True
                    continue
            # 500 에러 — 최대 3회 재시도 (1s → 2s → 3s)
            if res.status_code == 500 and attempt < 3:
                wait = attempt + 1
                logger.warning("KIS API 500 에러 — %d초 후 재시도 (%d/3) (%s)", wait, attempt + 1, url)
                time.sleep(wait)
                continue
            return res
        return res

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
            "FID_INPUT_PRICE_1": "1000",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        # 충분히 많이 가져와서 필터 후 n개 확보
        res = self._api_call("GET", url, "FHPST01710000", params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output", [])
        self._smart_sleep()
        result = []
        for item in output:
            ticker = item.get("mksc_shrn_iscd", "")
            name = item.get("hts_kor_isnm", "")
            if not ticker or _is_fund(name):
                continue
            if _to_int(item.get("stck_prpr")) < 1000:
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
        res = self._api_call("GET", url, "FHKST01010100", params=params, timeout=10)
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
                res = self._api_call("GET", url, tr_id, params=params, timeout=10)
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

    def get_daily_candles(self, ticker: str, days: int = 20) -> list[dict]:
        """일봉 조회 (TR: FHKST03010100) — 최근 N거래일 OHLCV + 등락률"""
        end = get_now_kst().strftime("%Y%m%d")
        # 충분한 날짜 범위 확보 (영업일 기준 days개를 확보하려면 달력일 기준으로 더 넓게)
        start = (get_now_kst() - timedelta(days=days * 2)).strftime("%Y%m%d")
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        res = self._api_call("GET", url, "FHKST03010100", params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output2", [])
        self._smart_sleep()

        result = []
        for r in reversed(output):  # API는 최신순, 오래된 순으로 뒤집기
            close = _to_int(r.get("stck_clpr"))
            if close == 0:
                continue
            result.append({
                "date": r.get("stck_bsop_date", ""),
                "open": _to_int(r.get("stck_oprc")),
                "high": _to_int(r.get("stck_hgpr")),
                "low": _to_int(r.get("stck_lwpr")),
                "close": close,
                "volume": _to_int(r.get("acml_vol")),
                "change_rate": _to_float(r.get("prdy_ctrt")),
            })
        return result[-days:]  # 최근 days개만 반환

    def get_weekly_candles(self, ticker: str, weeks: int = 20) -> list[dict]:
        """주봉 조회 (TR: FHKST03010100, 주봉) — 최근 N주 OHLCV"""
        end = get_now_kst().strftime("%Y%m%d")
        start = (get_now_kst() - timedelta(days=weeks * 7 + 30)).strftime("%Y%m%d")
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "W",
            "FID_ORG_ADJ_PRC": "0",
        }
        res = self._api_call("GET", url, "FHKST03010100", params=params, timeout=10)
        res.raise_for_status()
        output = res.json().get("output2", [])
        self._smart_sleep()

        result = []
        for r in reversed(output):
            close = _to_int(r.get("stck_clpr"))
            if close == 0:
                continue
            result.append({
                "date": r.get("stck_bsop_date", ""),
                "open": _to_int(r.get("stck_oprc")),
                "high": _to_int(r.get("stck_hgpr")),
                "low": _to_int(r.get("stck_lwpr")),
                "close": close,
                "volume": _to_int(r.get("acml_vol")),
            })
        return result[-weeks:]

    @staticmethod
    def calculate_bb_rsi(candles: list[dict], bb_period: int = 20, rsi_period: int = 14) -> dict:
        """일봉 캔들로 볼린저밴드 + RSI 계산 (종가 기준)"""
        min_required = max(bb_period, rsi_period + 1)
        if len(candles) < min_required:
            return {"error": f"캔들 부족: {len(candles)}개 (최소 {min_required}개 필요)"}

        closes = [c["close"] for c in candles]

        # Bollinger Bands
        recent = closes[-bb_period:]
        sma = sum(recent) / bb_period
        variance = sum((x - sma) ** 2 for x in recent) / bb_period
        std = variance ** 0.5
        bb_upper = sma + 2 * std
        bb_lower = sma - 2 * std

        # RSI (Wilder's smoothing)
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses[:rsi_period]) / rsi_period
        for i in range(rsi_period, len(gains)):
            avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period

        rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

        return {
            "bb_upper": round(bb_upper),
            "bb_middle": round(sma),
            "bb_lower": round(bb_lower),
            "bb_width_pct": round((bb_upper - bb_lower) / sma * 100, 2) if sma > 0 else 0,
            "rsi": round(rsi, 2),
        }

    def get_technical_indicators(self, ticker: str, daily: list[dict] | None = None) -> dict:
        """BB(20일), RSI(14일), 주봉 추세 통합 반환 — 스윙 트레이딩 진입/청산 판단용"""
        daily = daily if daily is not None else self.get_daily_candles(ticker, days=60)
        if len(daily) < 25:
            return {"ticker": ticker, "error": f"일봉 데이터 부족: {len(daily)}개"}

        tech = self.calculate_bb_rsi(daily)
        if "error" in tech:
            return {"ticker": ticker, **tech}

        # 현재가로 실시간 BB 위치 계산
        try:
            price_info = self.get_current_price(ticker)
            current_price = price_info["current_price"]
            tech["name"] = price_info.get("name", "")
            tech["change_rate"] = price_info.get("change_rate", 0)
        except Exception as e:
            logger.warning("현재가 조회 실패, 마지막 종가 사용 (%s): %s", ticker, e)
            current_price = daily[-1]["close"]
            tech["name"] = ""
            tech["change_rate"] = 0

        tech["current_price"] = current_price
        bb_upper = tech["bb_upper"]
        bb_lower = tech["bb_lower"]
        sma = tech["bb_middle"]
        band_half = (bb_upper - sma)  # = 2 * std

        if current_price <= bb_lower:
            bb_position = "below_lower"
        elif current_price <= bb_lower + band_half * 0.15:
            bb_position = "lower_touch"
        elif current_price >= bb_upper:
            bb_position = "above_upper"
        elif current_price >= bb_upper - band_half * 0.15:
            bb_position = "upper_touch"
        else:
            bb_position = "middle"
        tech["bb_position"] = bb_position

        # 주봉 추세 (최근 5주 vs 직전 5주 평균 비교)
        try:
            weekly = self.get_weekly_candles(ticker, weeks=20)
            if len(weekly) >= 10:
                recent_avg = sum(c["close"] for c in weekly[-5:]) / 5
                older_avg = sum(c["close"] for c in weekly[-10:-5]) / 5
                change_pct = (recent_avg - older_avg) / older_avg * 100 if older_avg > 0 else 0
                if change_pct >= 2.0:
                    weekly_trend = "up"
                elif change_pct <= -2.0:
                    weekly_trend = "down"
                else:
                    weekly_trend = "sideways"
                tech["weekly_trend"] = weekly_trend
                tech["weekly_change_pct"] = round(change_pct, 2)
            else:
                tech["weekly_trend"] = "unknown"
        except Exception as e:
            logger.warning("주봉 추세 조회 실패 (%s): %s", ticker, e)
            tech["weekly_trend"] = "unknown"

        return {"ticker": ticker, **tech}

    def get_market_regime(self, ticker: str = MARKET_PROXY_TICKER) -> dict:
        """코스피 대형주 프록시(KODEX 200)로 시장 레짐을 판별한다."""
        daily = self.get_daily_candles(ticker, days=80)
        if len(daily) < 60:
            return {
                "ticker": ticker,
                "status": "unknown",
                "buy_allowed": False,
                "recommended_buy_scale": 0.0,
                "reason": f"시장 데이터 부족: {len(daily)}개",
            }

        latest = daily[-1]
        close = latest["close"]
        ma20 = sum(c["close"] for c in daily[-20:]) / 20
        ma60 = sum(c["close"] for c in daily[-60:]) / 60
        change_rate = latest.get("change_rate", 0)
        if not change_rate and len(daily) >= 2 and daily[-2]["close"]:
            change_rate = (close - daily[-2]["close"]) / daily[-2]["close"] * 100

        if change_rate <= MARKET_CRASH_PCT:
            status = "crash"
            buy_allowed = False
            scale = 0.0
            reason = f"시장 급락({change_rate:.2f}% ≤ {MARKET_CRASH_PCT:.2f}%) — 신규 매수 금지"
        elif close < ma60:
            status = "risk_off"
            buy_allowed = False
            scale = 0.0
            reason = "60일선 하회 — 중기 하락 위험으로 신규 매수 금지"
        elif close < ma20:
            status = "caution"
            buy_allowed = True
            scale = 0.5
            reason = "20일선 하회/60일선 상회 — 절반 수량만 허용"
        else:
            status = "risk_on"
            buy_allowed = True
            scale = 1.0
            reason = "20일선·60일선 상회 — 정상 매수 가능"

        return {
            "ticker": ticker,
            "status": status,
            "buy_allowed": buy_allowed,
            "recommended_buy_scale": scale,
            "close": round(close, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "change_rate": round(change_rate, 2),
            "reason": reason,
        }

    def get_financial_summary(self, ticker: str) -> dict:
        """재무 요약 — 손익계산서 + 재무비율 통합 반환 (LLM용 단일 호출)"""
        try:
            income = self.get_income_statement(ticker, annual=True)
        except Exception as e:
            logger.warning("손익계산서 조회 실패 (%s): %s", ticker, e)
            income = []
        try:
            ratios = self.get_financial_ratio(ticker, annual=True)
        except Exception as e:
            logger.warning("재무비율 조회 실패 (%s): %s", ticker, e)
            ratios = []
        try:
            balance = self.get_balance_sheet(ticker, annual=True)
        except Exception as e:
            logger.warning("대차대조표 조회 실패 (%s): %s", ticker, e)
            balance = []

        # 최근 연도 기준으로 병합 (period 키 기준)
        merged: dict[str, dict] = {}
        for row in income:
            p = row["period"]
            merged.setdefault(p, {})["period"] = p
            merged[p].update({
                "revenue": row["revenue"],
                "operating_profit": row["operating_profit"],
                "net_profit": row["net_profit"],
                "operating_margin_pct": row["operating_margin_pct"],
                "eps": row["eps"],
            })
        for row in ratios:
            p = row["period"]
            merged.setdefault(p, {})["period"] = p
            merged[p].update({
                "roe_pct": row["roe_pct"],
                "per": row["per"],
                "pbr": row["pbr"],
            })
        for row in balance:
            p = row["period"]
            merged.setdefault(p, {})["period"] = p
            merged[p].update({
                "debt_ratio_pct": row["debt_ratio_pct"],
                "total_assets": row["total_assets"],
            })

        periods = sorted(merged.keys(), reverse=True)[:4]  # 최근 4개 연도
        return {"ticker": ticker, "annual": [merged[p] for p in periods]}

    # ── 잔고 조회 ─────────────────────────────────────────

    def get_balance(self) -> dict:
        """잔고 및 보유 종목 조회 (VTTC8434R 모의 / TTTC8434R 실투자)"""
        tr_id = "VTTC8434R" if KIS_MOCK else "TTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        # INQR_DVSN=02(개별) 실패 시 01(합산)으로 폴백
        for inqr_dvsn in ("02", "01"):
            params = {
                "CANO": self.acc_no,
                "ACNT_PRDT_CD": self.acc_suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": inqr_dvsn,
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }
            res = self._api_call("GET", url, tr_id, params=params, timeout=10)
            if res.status_code != 500:
                break
            logger.warning("잔고 조회 INQR_DVSN=%s 500 에러 — %s로 폴백",
                           inqr_dvsn, "01" if inqr_dvsn == "02" else "포기")
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
        res = self._api_call("POST", url, tr_id, json=body, timeout=10)
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
        res = self._api_call("POST", url, tr_id, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._smart_sleep()
        return {
            "success": data.get("rt_cd") == "0",
            "order_no": data.get("output", {}).get("odno", ""),
            "message": data.get("msg1", ""),
        }

    # ── 분봉 / 하이킨아시 ────────────────────────────────────

    def get_minute_candles(self, ticker: str, ha_candle_count: int = 60) -> dict:
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
        res = self._api_call("GET", url, "FHKST03010200", params=params, timeout=10)
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
        res = self._api_call("GET", url, tr_id, params=params, timeout=10)
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
        res = self._api_call("POST", url, tr_id, json=body, timeout=10)
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
                r["action"] = order["action"]
                r["quantity"] = order["remaining_qty"]
                r["price"] = order["order_price"]
                results.append(r)
                logger.info("주문 취소: %s %s → %s", order["ticker"], order["order_no"], r["message"])
            except Exception as e:
                logger.error("주문 취소 실패: %s %s → %s", order["ticker"], order["order_no"], e)
                results.append({
                    "success": False,
                    "order_no": order["order_no"],
                    "ticker": order["ticker"],
                    "name": order["name"],
                    "action": order["action"],
                    "quantity": order["remaining_qty"],
                    "price": order["order_price"],
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
        res = self._api_call("GET", url, tr_id, params=params, timeout=10)
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
