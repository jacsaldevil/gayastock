"""One-time real-account order smoke test.

The routine is intentionally separate from the trading strategy.  On one exact
KST date and within a narrow time window it buys one whitelisted, liquid ticker,
verifies the holding increase, then sells only that newly acquired quantity and
verifies that the position returned to its initial size.

It fails closed when GCS state persistence is unavailable because duplicate
orders are more dangerous than skipping the test.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, time as dtime, timedelta
from typing import Any, Protocol

from config import KIS_MOCK, MARKET_PROXY_TICKER
from data.trade_log import log_cancel, log_trade
from data.utils import get_now_kst

logger = logging.getLogger(__name__)

_DEFAULT_DATE = "2026-07-28"
_DEFAULT_TICKER = MARKET_PROXY_TICKER  # KODEX 200, high liquidity
_DEFAULT_QTY = 1
_DEFAULT_MAX_PRICE = 150_000
_DEFAULT_WINDOW_START = dtime(9, 20)
_DEFAULT_WINDOW_END = dtime(10, 30)
_TERMINAL_STATUSES = {
    "completed",
    "buy_rejected",
    "buy_not_filled",
    "price_limit_exceeded",
    "insufficient_cash",
    "failed_before_buy",
}


class StateStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...
    def save(self, state: dict[str, Any], *, create: bool = False) -> None: ...


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _parse_hhmm(raw: str, default: dtime) -> dtime:
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return dtime(hour, minute)
    except Exception:
        return default


def _holding_qty(portfolio: dict[str, Any], ticker: str) -> int:
    for holding in portfolio.get("holdings", []) or []:
        if str(holding.get("ticker", "")) == ticker:
            return int(holding.get("quantity", 0) or 0)
    return 0


def _holding(portfolio: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    return next(
        (holding for holding in portfolio.get("holdings", []) or [] if str(holding.get("ticker", "")) == ticker),
        None,
    )


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key not in {"owner", "lease_until"}
    }


class GCSStateStore:
    """Generation-guarded GCS state used to prevent duplicate real orders."""

    def __init__(self, test_date: str):
        bucket_name = os.environ.get("GCS_DATA_BUCKET", "").strip()
        if not bucket_name:
            raise RuntimeError("GCS_DATA_BUCKET 미설정 — 중복 주문 방지를 보장할 수 없음")
        from google.cloud import storage

        self._blob = storage.Client().bucket(bucket_name).blob(
            f"ops/live_order_smoke_test_{test_date.replace('-', '')}.json"
        )
        self._generation: int | None = None

    def load(self) -> dict[str, Any] | None:
        try:
            self._blob.reload()
            self._generation = int(self._blob.generation or 0)
            return json.loads(self._blob.download_as_text(encoding="utf-8"))
        except Exception as exc:
            if "404" in str(exc) or "NotFound" in type(exc).__name__:
                self._generation = 0
                return None
            raise

    def save(self, state: dict[str, Any], *, create: bool = False) -> None:
        generation = 0 if create else self._generation
        if generation is None:
            raise RuntimeError("상태를 load하지 않고 save할 수 없음")
        self._blob.upload_from_string(
            json.dumps(state, ensure_ascii=False, indent=2),
            content_type="application/json; charset=utf-8",
            if_generation_match=generation,
        )
        self._blob.reload()
        self._generation = int(self._blob.generation or 0)


class MemoryStateStore:
    """Test helper; not used for live operation."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state

    def load(self) -> dict[str, Any] | None:
        return None if self.state is None else dict(self.state)

    def save(self, state: dict[str, Any], *, create: bool = False) -> None:
        if create and self.state is not None:
            raise RuntimeError("state already exists")
        self.state = dict(state)


def _poll_holding_qty(broker, ticker: str, attempts: int, sleep_sec: float, sleep_fn) -> tuple[int, dict]:
    portfolio: dict[str, Any] = {}
    qty = 0
    for attempt in range(attempts):
        portfolio = broker.get_balance()
        qty = _holding_qty(portfolio, ticker)
        if attempt < attempts - 1:
            sleep_fn(sleep_sec)
    return qty, portfolio


def _matching_pending(broker, ticker: str, action: str) -> list[dict[str, Any]]:
    try:
        return [
            order for order in broker.get_pending_orders()
            if str(order.get("ticker", "")) == ticker and str(order.get("action", "")) == action
        ]
    except Exception as exc:
        logger.warning("스모크 테스트 미체결 조회 실패: %s", exc)
        return []


def _save(store: StateStore, state: dict[str, Any], now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    state["lease_until"] = (now + timedelta(minutes=5)).isoformat()
    store.save(state)


def _claim(store: StateStore, now: datetime, test_date: str, ticker: str, qty: int) -> tuple[dict[str, Any] | None, str]:
    owner = uuid.uuid4().hex
    state = store.load()
    if state is None:
        state = {
            "test_date": test_date,
            "ticker": ticker,
            "requested_qty": qty,
            "status": "claimed",
            "owner": owner,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "lease_until": (now + timedelta(minutes=5)).isoformat(),
        }
        try:
            store.save(state, create=True)
            return state, "claimed"
        except Exception as exc:
            logger.warning("스모크 테스트 최초 상태 선점 실패: %s", exc)
            return None, "busy"

    if state.get("status") in _TERMINAL_STATUSES:
        return state, "terminal"

    try:
        lease_until = datetime.fromisoformat(str(state.get("lease_until", "")))
    except Exception:
        lease_until = now - timedelta(seconds=1)
    if lease_until > now and state.get("owner"):
        return state, "busy"

    state["owner"] = owner
    state["lease_until"] = (now + timedelta(minutes=5)).isoformat()
    state["updated_at"] = now.isoformat()
    try:
        store.save(state)
    except Exception as exc:
        logger.warning("스모크 테스트 상태 재선점 실패: %s", exc)
        return None, "busy"
    return state, "resumed"


def run_live_order_smoke_test(
    broker,
    *,
    now: datetime | None = None,
    store: StateStore | None = None,
    sleep_fn=time.sleep,
) -> dict[str, Any]:
    """Run or resume the one-time buy/fill/sell/fill verification routine."""
    now = now or get_now_kst()
    enabled = _env_bool("LIVE_SMOKE_TEST_ENABLED", False)
    test_date = os.environ.get("LIVE_SMOKE_TEST_DATE", _DEFAULT_DATE).strip()
    ticker = os.environ.get("LIVE_SMOKE_TEST_TICKER", _DEFAULT_TICKER).strip()
    qty = max(1, min(int(os.environ.get("LIVE_SMOKE_TEST_QTY", _DEFAULT_QTY)), 1))
    max_price = int(os.environ.get("LIVE_SMOKE_TEST_MAX_PRICE", _DEFAULT_MAX_PRICE))
    window_start = _parse_hhmm(os.environ.get("LIVE_SMOKE_TEST_WINDOW_START", "09:20"), _DEFAULT_WINDOW_START)
    window_end = _parse_hhmm(os.environ.get("LIVE_SMOKE_TEST_WINDOW_END", "10:30"), _DEFAULT_WINDOW_END)

    if not enabled:
        return {"attempted": False, "status": "disabled", "halt_agent": False}
    if os.environ.get("DRY_RUN", "false").lower() == "true":
        return {"attempted": False, "status": "dry_run", "halt_agent": False}
    if KIS_MOCK:
        return {"attempted": False, "status": "mock_account", "halt_agent": False}
    if now.date().isoformat() != test_date:
        return {"attempted": False, "status": "date_mismatch", "halt_agent": False}
    if not (window_start <= now.time() <= window_end):
        return {"attempted": False, "status": "outside_window", "halt_agent": False}
    if len(ticker) != 6 or not ticker.isdigit():
        return {"attempted": False, "status": "invalid_ticker", "halt_agent": False}

    try:
        store = store or GCSStateStore(test_date)
    except Exception as exc:
        logger.error("실주문 스모크 테스트 차단: %s", exc)
        return {
            "attempted": False,
            "status": "state_store_unavailable",
            "halt_agent": False,
            "error": str(exc),
        }

    state, claim_status = _claim(store, now, test_date, ticker, qty)
    if state is None:
        return {"attempted": False, "status": claim_status, "halt_agent": False}
    if claim_status in {"terminal", "busy"}:
        return {
            "attempted": False,
            "status": state.get("status", claim_status),
            "halt_agent": state.get("status") not in _TERMINAL_STATUSES,
            "state": _public_state(state),
        }

    logger.warning("실계좌 주문 스모크 테스트 시작: %s %d주", ticker, qty)
    try:
        if state.get("initial_qty") is None:
            initial_portfolio = broker.get_balance()
            state["initial_qty"] = _holding_qty(initial_portfolio, ticker)
            state["initial_cash"] = int(initial_portfolio.get("cash", 0) or 0)
            _save(store, state, now)

        initial_qty = int(state.get("initial_qty", 0) or 0)

        if state.get("status") == "claimed":
            price_info = broker.get_current_price(ticker)
            current_price = int(price_info.get("current_price", 0) or 0)
            stock_name = str(price_info.get("name", "") or ticker)
            state["stock_name"] = stock_name
            state["buy_reference_price"] = current_price

            if current_price <= 0 or current_price > max_price:
                state["status"] = "price_limit_exceeded"
                state["error"] = f"현재가 {current_price:,}원, 허용 상한 {max_price:,}원"
                _save(store, state, now)
                return {"attempted": False, "status": state["status"], "halt_agent": False, "state": _public_state(state)}
            if int(state.get("initial_cash", 0) or 0) < current_price * qty:
                state["status"] = "insufficient_cash"
                state["error"] = f"예수금 {state.get('initial_cash', 0):,}원 < 필요금액 {current_price * qty:,}원"
                _save(store, state, now)
                return {"attempted": False, "status": state["status"], "halt_agent": False, "state": _public_state(state)}

            buy_result = broker.buy_order(ticker, qty)
            state["buy_order"] = buy_result
            state["buy_order_no"] = str(buy_result.get("order_no", ""))
            if not buy_result.get("success"):
                state["status"] = "buy_rejected"
                _save(store, state, now)
                log_trade(
                    "BUY", ticker, qty, current_price,
                    f"[LIVE-SMOKE-TEST] 매수 주문 거절: {buy_result.get('message', '')}",
                    False, stock_name,
                )
                return {"attempted": True, "status": state["status"], "halt_agent": False, "state": _public_state(state)}
            state["status"] = "buy_submitted"
            _save(store, state, now)

        if state.get("status") == "buy_submitted":
            after_buy_qty, after_buy_portfolio = _poll_holding_qty(broker, ticker, 6, 2.0, sleep_fn)
            bought_qty = max(0, after_buy_qty - initial_qty)
            if bought_qty <= 0:
                pending_buys = _matching_pending(broker, ticker, "BUY")
                for order in pending_buys:
                    cancel_result = broker.cancel_order(
                        str(order.get("order_no", "")),
                        str(order.get("krx_fwdg_ord_orgno", "")),
                    )
                    if cancel_result.get("success"):
                        log_cancel(
                            ticker, int(order.get("remaining_qty", qty) or qty),
                            int(order.get("order_price", 0) or 0), str(order.get("order_no", "")),
                            str(state.get("stock_name", "")),
                        )
                sleep_fn(2.0)
                after_cancel_portfolio = broker.get_balance()
                bought_qty = max(0, _holding_qty(after_cancel_portfolio, ticker) - initial_qty)
                after_buy_portfolio = after_cancel_portfolio

            if bought_qty <= 0:
                state["status"] = "buy_not_filled"
                state["bought_qty"] = 0
                _save(store, state, now)
                return {"attempted": True, "status": state["status"], "halt_agent": False, "state": _public_state(state)}

            bought_qty = min(bought_qty, qty)
            holding = _holding(after_buy_portfolio, ticker) or {}
            buy_price = int(float(holding.get("avg_price", state.get("buy_reference_price", 0)) or 0))
            state["bought_qty"] = bought_qty
            state["buy_fill_price"] = buy_price
            state["status"] = "buy_filled"
            if not state.get("buy_logged"):
                log_trade(
                    "BUY", ticker, bought_qty, buy_price,
                    f"[LIVE-SMOKE-TEST {test_date}] KIS 매수 주문 및 잔고 반영 검증",
                    True, str(state.get("stock_name", "")),
                )
                state["buy_logged"] = True
            _save(store, state, now)

        if state.get("status") in {"buy_filled", "sell_rejected"}:
            bought_qty = int(state.get("bought_qty", 0) or 0)
            if bought_qty <= 0:
                state["status"] = "failed_before_buy"
                state["error"] = "매도할 테스트 체결 수량이 없음"
                _save(store, state, now)
                return {"attempted": True, "status": state["status"], "halt_agent": False, "state": _public_state(state)}

            sell_result = broker.sell_order(ticker, bought_qty)
            state["sell_order"] = sell_result
            state["sell_order_no"] = str(sell_result.get("order_no", ""))
            state["sell_attempts"] = int(state.get("sell_attempts", 0) or 0) + 1
            if not sell_result.get("success"):
                state["status"] = "sell_rejected"
                _save(store, state, now)
                log_trade(
                    "SELL", ticker, bought_qty, int(state.get("buy_fill_price", 0) or 0),
                    f"[LIVE-SMOKE-TEST] 매도 주문 거절: {sell_result.get('message', '')}",
                    False, str(state.get("stock_name", "")),
                )
                return {"attempted": True, "status": state["status"], "halt_agent": True, "state": _public_state(state)}
            state["status"] = "sell_submitted"
            _save(store, state, now)

        if state.get("status") in {"sell_submitted", "sell_pending"}:
            final_qty, final_portfolio = _poll_holding_qty(broker, ticker, 6, 2.0, sleep_fn)
            if final_qty > initial_qty:
                pending_sells = _matching_pending(broker, ticker, "SELL")
                state["status"] = "sell_pending" if pending_sells else "sell_rejected"
                state["remaining_test_qty"] = final_qty - initial_qty
                _save(store, state, now)
                return {"attempted": True, "status": state["status"], "halt_agent": True, "state": _public_state(state)}

            sell_price_info = broker.get_current_price(ticker)
            sell_price = int(sell_price_info.get("current_price", 0) or 0)
            buy_price = int(state.get("buy_fill_price", state.get("buy_reference_price", 0)) or 0)
            bought_qty = int(state.get("bought_qty", qty) or qty)
            if not state.get("sell_logged"):
                log_trade(
                    "SELL", ticker, bought_qty, sell_price,
                    f"[LIVE-SMOKE-TEST {test_date}] KIS 매도 주문 및 잔고 원복 검증",
                    True, str(state.get("stock_name", "")),
                    profit=(sell_price - buy_price) * bought_qty,
                )
                state["sell_logged"] = True
            state["status"] = "completed"
            state["final_qty"] = final_qty
            state["final_cash"] = int(final_portfolio.get("cash", 0) or 0)
            state["sell_reference_price"] = sell_price
            state["completed_at"] = now.isoformat()
            _save(store, state, now)
            logger.warning("실계좌 주문 스모크 테스트 완료: %s %d주 왕복", ticker, bought_qty)
            return {"attempted": True, "status": "completed", "halt_agent": False, "state": _public_state(state)}

        return {
            "attempted": True,
            "status": str(state.get("status", "unknown")),
            "halt_agent": str(state.get("status", "")) not in _TERMINAL_STATUSES,
            "state": _public_state(state),
        }
    except Exception as exc:
        logger.exception("실계좌 주문 스모크 테스트 오류")
        state["error"] = str(exc)
        if int(state.get("bought_qty", 0) or 0) > 0:
            state["status"] = "recovery_required"
            halt_agent = True
        else:
            state["status"] = "failed_before_buy"
            halt_agent = False
        try:
            _save(store, state, now)
        except Exception:
            pass
        return {
            "attempted": True,
            "status": state["status"],
            "halt_agent": halt_agent,
            "error": str(exc),
            "state": _public_state(state),
        }
