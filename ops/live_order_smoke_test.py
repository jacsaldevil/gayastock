"""One-time real-account buy/sell smoke test with duplicate-order safeguards."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, time as dtime, timedelta
from typing import Any, Protocol

from data.trade_log import log_cancel, log_trade
from data.utils import get_now_kst

logger = logging.getLogger(__name__)

_DEFAULT_DATE = "2026-07-28"
_DEFAULT_TICKER = "069500"  # KODEX 200
_DEFAULT_MAX_PRICE = 150_000
_DEFAULT_WINDOW_START = dtime(9, 20)
_DEFAULT_WINDOW_END = dtime(10, 30)
_MAX_SELL_ATTEMPTS = 2
_TERMINAL = {
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


class GCSStateStore:
    """Generation-guarded GCS state. No state store means no live order."""

    def __init__(self, test_date: str):
        bucket_name = os.environ.get("GCS_DATA_BUCKET", "").strip()
        if not bucket_name:
            raise RuntimeError("GCS_DATA_BUCKET 미설정 — 중복 주문 방지 불가")
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
            raise RuntimeError("상태 load 없이 save할 수 없음")
        self._blob.upload_from_string(
            json.dumps(state, ensure_ascii=False, indent=2),
            content_type="application/json; charset=utf-8",
            if_generation_match=generation,
        )
        self._blob.reload()
        self._generation = int(self._blob.generation or 0)


class MemoryStateStore:
    """Unit-test helper only."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state

    def load(self) -> dict[str, Any] | None:
        return None if self.state is None else dict(self.state)

    def save(self, state: dict[str, Any], *, create: bool = False) -> None:
        if create and self.state is not None:
            raise RuntimeError("state already exists")
        self.state = dict(state)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _parse_hhmm(raw: str, default: dtime) -> dtime:
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return dtime(hour, minute)
    except Exception:
        return default


def _qty(portfolio: dict[str, Any], ticker: str) -> int:
    for holding in portfolio.get("holdings", []) or []:
        if str(holding.get("ticker", "")) == ticker:
            return int(holding.get("quantity", 0) or 0)
    return 0


def _holding(portfolio: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    return next(
        (h for h in portfolio.get("holdings", []) or [] if str(h.get("ticker", "")) == ticker),
        None,
    )


def _public(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if k not in {"owner", "lease_until"}}


def _save(store: StateStore, state: dict[str, Any], now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    state["lease_until"] = (now + timedelta(minutes=5)).isoformat()
    store.save(state)


def _claim(
    store: StateStore,
    now: datetime,
    test_date: str,
    ticker: str,
) -> tuple[dict[str, Any] | None, str]:
    state = store.load()
    owner = uuid.uuid4().hex
    if state is None:
        state = {
            "test_date": test_date,
            "ticker": ticker,
            "requested_qty": 1,
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
            logger.warning("스모크 테스트 상태 선점 실패: %s", exc)
            return None, "busy"

    if state.get("status") in _TERMINAL:
        return state, "terminal"
    try:
        lease_until = datetime.fromisoformat(str(state.get("lease_until", "")))
    except Exception:
        lease_until = now - timedelta(seconds=1)
    if state.get("owner") and lease_until > now:
        return state, "busy"

    state["owner"] = owner
    state["lease_until"] = (now + timedelta(minutes=5)).isoformat()
    state["updated_at"] = now.isoformat()
    try:
        store.save(state)
        return state, "resumed"
    except Exception as exc:
        logger.warning("스모크 테스트 상태 재선점 실패: %s", exc)
        return None, "busy"


def _pending(broker, ticker: str, action: str) -> list[dict[str, Any]]:
    try:
        return [
            order for order in broker.get_pending_orders()
            if str(order.get("ticker", "")) == ticker and str(order.get("action", "")) == action
        ]
    except Exception as exc:
        logger.warning("미체결 조회 실패: %s", exc)
        return []


def _poll(broker, ticker: str, predicate, sleep_fn) -> tuple[int, dict[str, Any]]:
    portfolio: dict[str, Any] = {}
    observed = 0
    for attempt in range(6):
        portfolio = broker.get_balance()
        observed = _qty(portfolio, ticker)
        if predicate(observed):
            break
        if attempt < 5:
            sleep_fn(2.0)
    return observed, portfolio


def _record_buy_fill(
    store: StateStore,
    state: dict[str, Any],
    portfolio: dict[str, Any],
    ticker: str,
    initial_qty: int,
    test_date: str,
    now: datetime,
) -> int:
    bought_qty = min(max(0, _qty(portfolio, ticker) - initial_qty), 1)
    if bought_qty <= 0:
        return 0
    holding = _holding(portfolio, ticker) or {}
    buy_price = int(float(holding.get("avg_price", state.get("buy_reference_price", 0)) or 0))
    state.update({"status": "buy_filled", "bought_qty": bought_qty, "buy_fill_price": buy_price})
    if not state.get("buy_logged"):
        log_trade(
            "BUY", ticker, bought_qty, buy_price,
            f"[LIVE-SMOKE-TEST {test_date}] KIS 매수 주문 및 잔고 반영 검증",
            True, str(state.get("stock_name", "")),
        )
        state["buy_logged"] = True
    _save(store, state, now)
    return bought_qty


def _complete(
    store: StateStore,
    state: dict[str, Any],
    broker,
    portfolio: dict[str, Any],
    ticker: str,
    initial_qty: int,
    test_date: str,
    now: datetime,
) -> dict[str, Any]:
    final_qty = _qty(portfolio, ticker)
    if final_qty != initial_qty:
        raise RuntimeError(f"잔고 원복 실패: initial={initial_qty}, final={final_qty}")
    sell_price = int(broker.get_current_price(ticker).get("current_price", 0) or 0)
    buy_price = int(state.get("buy_fill_price", state.get("buy_reference_price", 0)) or 0)
    bought_qty = int(state.get("bought_qty", 1) or 1)
    if not state.get("sell_logged"):
        log_trade(
            "SELL", ticker, bought_qty, sell_price,
            f"[LIVE-SMOKE-TEST {test_date}] KIS 매도 주문 및 잔고 원복 검증",
            True, str(state.get("stock_name", "")),
            profit=(sell_price - buy_price) * bought_qty,
        )
        state["sell_logged"] = True
    state.update({
        "status": "completed",
        "final_qty": final_qty,
        "final_cash": int(portfolio.get("cash", 0) or 0),
        "sell_reference_price": sell_price,
        "quantity_restored": True,
        "completed_at": now.isoformat(),
    })
    _save(store, state, now)
    logger.warning("실계좌 스모크 테스트 완료: %s 1주 매수·매도", ticker)
    return {"attempted": True, "status": "completed", "halt_agent": False, "state": _public(state)}


def run_live_order_smoke_test(
    broker,
    *,
    now: datetime | None = None,
    store: StateStore | None = None,
    sleep_fn=time.sleep,
) -> dict[str, Any]:
    """Run or resume the one-time real-account order verification."""
    now = now or get_now_kst()
    test_date = os.environ.get("LIVE_SMOKE_TEST_DATE", _DEFAULT_DATE).strip()
    ticker = os.environ.get("LIVE_SMOKE_TEST_TICKER", _DEFAULT_TICKER).strip()
    max_price = int(os.environ.get("LIVE_SMOKE_TEST_MAX_PRICE", _DEFAULT_MAX_PRICE))
    window_start = _parse_hhmm(os.environ.get("LIVE_SMOKE_TEST_WINDOW_START", "09:20"), _DEFAULT_WINDOW_START)
    window_end = _parse_hhmm(os.environ.get("LIVE_SMOKE_TEST_WINDOW_END", "10:30"), _DEFAULT_WINDOW_END)

    if not _env_bool("LIVE_SMOKE_TEST_ENABLED"):
        return {"attempted": False, "status": "disabled", "halt_agent": False}
    if _env_bool("DRY_RUN"):
        return {"attempted": False, "status": "dry_run", "halt_agent": False}
    if _env_bool("KIS_MOCK", True):
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
        return {"attempted": False, "status": "state_store_unavailable", "halt_agent": False, "error": str(exc)}

    state, claim_status = _claim(store, now, test_date, ticker)
    if state is None:
        return {"attempted": False, "status": claim_status, "halt_agent": False}
    if claim_status in {"terminal", "busy"}:
        return {
            "attempted": False,
            "status": str(state.get("status", claim_status)),
            "halt_agent": str(state.get("status", "")) not in _TERMINAL,
            "state": _public(state),
        }

    try:
        if state.get("initial_qty") is None:
            initial = broker.get_balance()
            state["initial_qty"] = _qty(initial, ticker)
            state["initial_cash"] = int(initial.get("cash", 0) or 0)
            _save(store, state, now)
        initial_qty = int(state.get("initial_qty", 0) or 0)

        if state.get("status") == "claimed":
            price_info = broker.get_current_price(ticker)
            price = int(price_info.get("current_price", 0) or 0)
            state.update({"stock_name": str(price_info.get("name", "") or ticker), "buy_reference_price": price})
            if price <= 0 or price > max_price:
                state.update({"status": "price_limit_exceeded", "error": f"현재가 {price:,}원, 상한 {max_price:,}원"})
                _save(store, state, now)
                return {"attempted": False, "status": state["status"], "halt_agent": False, "state": _public(state)}
            if int(state.get("initial_cash", 0) or 0) < price:
                state.update({"status": "insufficient_cash", "error": f"예수금 부족: {state.get('initial_cash', 0):,}원"})
                _save(store, state, now)
                return {"attempted": False, "status": state["status"], "halt_agent": False, "state": _public(state)}
            state.update({"status": "buy_intent", "buy_intent_at": now.isoformat()})
            _save(store, state, now)

        if state.get("status") == "buy_intent":
            portfolio = broker.get_balance()
            if _record_buy_fill(store, state, portfolio, ticker, initial_qty, test_date, now) <= 0:
                pending_buy = _pending(broker, ticker, "BUY")
                if pending_buy:
                    state.update({"status": "buy_submitted", "buy_order_no": str(pending_buy[0].get("order_no", ""))})
                    _save(store, state, now)
                else:
                    result = broker.buy_order(ticker, 1)
                    state.update({"buy_order": result, "buy_order_no": str(result.get("order_no", ""))})
                    if not result.get("success"):
                        state["status"] = "buy_rejected"
                        _save(store, state, now)
                        log_trade(
                            "BUY", ticker, 1, int(state.get("buy_reference_price", 0) or 0),
                            f"[LIVE-SMOKE-TEST] 매수 주문 거절: {result.get('message', '')}",
                            False, str(state.get("stock_name", "")),
                        )
                        return {"attempted": True, "status": "buy_rejected", "halt_agent": False, "state": _public(state)}
                    state["status"] = "buy_submitted"
                    _save(store, state, now)

        if state.get("status") == "buy_submitted":
            _, portfolio = _poll(broker, ticker, lambda observed: observed > initial_qty, sleep_fn)
            if _record_buy_fill(store, state, portfolio, ticker, initial_qty, test_date, now) <= 0:
                for order in _pending(broker, ticker, "BUY"):
                    cancelled = broker.cancel_order(
                        str(order.get("order_no", "")), str(order.get("krx_fwdg_ord_orgno", ""))
                    )
                    if cancelled.get("success"):
                        log_cancel(
                            ticker, int(order.get("remaining_qty", 1) or 1),
                            int(order.get("order_price", 0) or 0), str(order.get("order_no", "")),
                            str(state.get("stock_name", "")),
                        )
                sleep_fn(2.0)
                portfolio = broker.get_balance()
                if _record_buy_fill(store, state, portfolio, ticker, initial_qty, test_date, now) <= 0:
                    state.update({"status": "buy_not_filled", "bought_qty": 0})
                    _save(store, state, now)
                    return {"attempted": True, "status": "buy_not_filled", "halt_agent": False, "state": _public(state)}

        if state.get("status") == "buy_filled":
            state.update({"status": "sell_intent", "sell_intent_at": now.isoformat()})
            _save(store, state, now)

        if state.get("status") in {"sell_intent", "sell_rejected"}:
            portfolio = broker.get_balance()
            observed_qty = _qty(portfolio, ticker)
            if observed_qty == initial_qty:
                return _complete(store, state, broker, portfolio, ticker, initial_qty, test_date, now)
            pending_sell = _pending(broker, ticker, "SELL")
            if pending_sell:
                state.update({"status": "sell_submitted", "sell_order_no": str(pending_sell[0].get("order_no", ""))})
                _save(store, state, now)
            else:
                attempts = int(state.get("sell_attempts", 0) or 0)
                if attempts >= _MAX_SELL_ATTEMPTS:
                    state.update({
                        "status": "recovery_required",
                        "remaining_test_qty": max(0, observed_qty - initial_qty),
                        "error": "매도 재시도 한도 초과",
                    })
                    _save(store, state, now)
                    return {"attempted": True, "status": "recovery_required", "halt_agent": True, "state": _public(state)}
                sell_qty = min(int(state.get("bought_qty", 1) or 1), observed_qty - initial_qty)
                state.update({"sell_attempts": attempts + 1, "status": "sell_intent"})
                _save(store, state, now)
                result = broker.sell_order(ticker, sell_qty)
                state.update({"sell_order": result, "sell_order_no": str(result.get("order_no", ""))})
                if not result.get("success"):
                    state["status"] = "sell_rejected"
                    _save(store, state, now)
                    log_trade(
                        "SELL", ticker, sell_qty, int(state.get("buy_fill_price", 0) or 0),
                        f"[LIVE-SMOKE-TEST] 매도 주문 거절: {result.get('message', '')}",
                        False, str(state.get("stock_name", "")),
                    )
                    return {"attempted": True, "status": "sell_rejected", "halt_agent": True, "state": _public(state)}
                state["status"] = "sell_submitted"
                _save(store, state, now)

        if state.get("status") in {"sell_submitted", "sell_pending"}:
            final_qty, portfolio = _poll(broker, ticker, lambda observed: observed == initial_qty, sleep_fn)
            if final_qty == initial_qty:
                return _complete(store, state, broker, portfolio, ticker, initial_qty, test_date, now)
            pending_sell = _pending(broker, ticker, "SELL")
            state.update({
                "status": "sell_pending" if pending_sell else "sell_intent",
                "remaining_test_qty": max(0, final_qty - initial_qty),
            })
            _save(store, state, now)
            return {"attempted": True, "status": state["status"], "halt_agent": True, "state": _public(state)}

        status = str(state.get("status", "unknown"))
        return {"attempted": True, "status": status, "halt_agent": status not in _TERMINAL, "state": _public(state)}
    except Exception as exc:
        logger.exception("실계좌 주문 스모크 테스트 오류")
        state["error"] = str(exc)
        try:
            current_qty = _qty(broker.get_balance(), ticker)
        except Exception:
            current_qty = int(state.get("initial_qty", 0) or 0)
        initial_qty = int(state.get("initial_qty", 0) or 0)
        if current_qty > initial_qty:
            state.update({"status": "recovery_required", "remaining_test_qty": current_qty - initial_qty})
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
            "state": _public(state),
        }
