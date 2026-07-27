import os
import unittest
from datetime import datetime
from unittest.mock import patch

from ops.live_order_smoke_test import MemoryStateStore, run_live_order_smoke_test


class FakeBroker:
    def __init__(self, *, fill_buy=True, fill_sell=True, initial_qty=0):
        self.qty = initial_qty
        self.cash = 283_616 - max(0, initial_qty) * 0
        self.fill_buy = fill_buy
        self.fill_sell = fill_sell
        self.buy_calls = 0
        self.sell_calls = 0
        self.cancel_calls = 0
        self.pending = []

    def get_balance(self):
        holdings = []
        if self.qty > 0:
            holdings.append({
                "ticker": "069500",
                "name": "KODEX 200",
                "quantity": self.qty,
                "avg_price": 107_000,
                "current_price": 107_000,
            })
        return {
            "cash": self.cash,
            "holdings_eval": self.qty * 107_000,
            "total_eval": self.cash + self.qty * 107_000,
            "holdings": holdings,
        }

    def get_current_price(self, ticker):
        return {"ticker": ticker, "name": "KODEX 200", "current_price": 107_000}

    def buy_order(self, ticker, quantity, price=0):
        self.buy_calls += 1
        if self.fill_buy:
            self.qty += quantity
            self.cash -= 107_000 * quantity
        else:
            self.pending = [{
                "order_no": "BUY-1",
                "krx_fwdg_ord_orgno": "",
                "ticker": ticker,
                "action": "BUY",
                "remaining_qty": quantity,
                "order_price": 0,
            }]
        return {"success": True, "order_no": "BUY-1", "message": "accepted"}

    def sell_order(self, ticker, quantity, price=0):
        self.sell_calls += 1
        if self.fill_sell:
            self.qty -= quantity
            self.cash += 107_000 * quantity
        else:
            self.pending = [{
                "order_no": "SELL-1",
                "krx_fwdg_ord_orgno": "",
                "ticker": ticker,
                "action": "SELL",
                "remaining_qty": quantity,
                "order_price": 0,
            }]
        return {"success": True, "order_no": "SELL-1", "message": "accepted"}

    def get_pending_orders(self):
        return list(self.pending)

    def cancel_order(self, order_no, krx_fwdg_ord_orgno=""):
        self.cancel_calls += 1
        self.pending = []
        return {"success": True, "order_no": order_no, "message": "cancelled"}


@patch("ops.live_order_smoke_test.KIS_MOCK", False)
class LiveOrderSmokeTest(unittest.TestCase):
    def env(self):
        return patch.dict(os.environ, {
            "LIVE_SMOKE_TEST_ENABLED": "true",
            "LIVE_SMOKE_TEST_DATE": "2026-07-28",
            "LIVE_SMOKE_TEST_TICKER": "069500",
            "LIVE_SMOKE_TEST_QTY": "1",
            "LIVE_SMOKE_TEST_MAX_PRICE": "150000",
            "LIVE_SMOKE_TEST_WINDOW_START": "09:20",
            "LIVE_SMOKE_TEST_WINDOW_END": "10:30",
            "DRY_RUN": "false",
        }, clear=False)

    @patch("ops.live_order_smoke_test.log_trade")
    @patch("ops.live_order_smoke_test.log_cancel")
    def test_successful_round_trip_verifies_balance(self, _log_cancel, log_trade):
        broker = FakeBroker()
        store = MemoryStateStore()
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 28, 9, 30),
                store=store,
                sleep_fn=lambda _: None,
            )
        self.assertTrue(result["attempted"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["halt_agent"])
        self.assertEqual(broker.buy_calls, 1)
        self.assertEqual(broker.sell_calls, 1)
        self.assertEqual(broker.qty, 0)
        self.assertEqual(store.state["initial_qty"], store.state["final_qty"])
        self.assertTrue(store.state["quantity_restored"])
        self.assertEqual(log_trade.call_count, 2)

    @patch("ops.live_order_smoke_test.log_trade")
    @patch("ops.live_order_smoke_test.log_cancel")
    def test_unfilled_buy_is_cancelled_without_sell(self, log_cancel, _log_trade):
        broker = FakeBroker(fill_buy=False)
        store = MemoryStateStore()
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 28, 9, 30),
                store=store,
                sleep_fn=lambda _: None,
            )
        self.assertEqual(result["status"], "buy_not_filled")
        self.assertFalse(result["halt_agent"])
        self.assertEqual(broker.buy_calls, 1)
        self.assertEqual(broker.sell_calls, 0)
        self.assertEqual(broker.cancel_calls, 1)
        log_cancel.assert_called_once()

    def test_completed_state_prevents_duplicate_order(self):
        broker = FakeBroker()
        store = MemoryStateStore({
            "status": "completed",
            "test_date": "2026-07-28",
            "ticker": "069500",
            "initial_qty": 0,
            "final_qty": 0,
        })
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 28, 10, 0),
                store=store,
                sleep_fn=lambda _: None,
            )
        self.assertFalse(result["attempted"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(broker.buy_calls, 0)
        self.assertEqual(broker.sell_calls, 0)

    def test_wrong_date_never_touches_broker(self):
        broker = FakeBroker()
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 29, 9, 30),
                store=MemoryStateStore(),
                sleep_fn=lambda _: None,
            )
        self.assertFalse(result["attempted"])
        self.assertEqual(result["status"], "date_mismatch")
        self.assertEqual(broker.buy_calls, 0)
        self.assertEqual(broker.sell_calls, 0)

    @patch("ops.live_order_smoke_test.log_trade")
    @patch("ops.live_order_smoke_test.log_cancel")
    def test_existing_holding_returns_to_original_quantity(self, _log_cancel, _log_trade):
        broker = FakeBroker(initial_qty=2)
        store = MemoryStateStore()
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 28, 9, 30),
                store=store,
                sleep_fn=lambda _: None,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(broker.qty, 2)
        self.assertEqual(store.state["initial_qty"], 2)
        self.assertEqual(store.state["final_qty"], 2)

    @patch("ops.live_order_smoke_test.log_trade")
    @patch("ops.live_order_smoke_test.log_cancel")
    def test_resume_buy_intent_detects_fill_without_duplicate_buy(self, _log_cancel, _log_trade):
        broker = FakeBroker(initial_qty=1)
        store = MemoryStateStore({
            "status": "buy_intent",
            "test_date": "2026-07-28",
            "ticker": "069500",
            "requested_qty": 1,
            "initial_qty": 0,
            "initial_cash": 283_616,
            "stock_name": "KODEX 200",
            "buy_reference_price": 107_000,
            "owner": "stale-owner",
            "lease_until": "2026-07-28T09:35:00",
        })
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 28, 10, 0),
                store=store,
                sleep_fn=lambda _: None,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(broker.buy_calls, 0)
        self.assertEqual(broker.sell_calls, 1)
        self.assertEqual(broker.qty, 0)

    @patch("ops.live_order_smoke_test.log_trade")
    @patch("ops.live_order_smoke_test.log_cancel")
    def test_resume_sell_intent_detects_already_completed_sell(self, _log_cancel, _log_trade):
        broker = FakeBroker(initial_qty=0)
        store = MemoryStateStore({
            "status": "sell_intent",
            "test_date": "2026-07-28",
            "ticker": "069500",
            "requested_qty": 1,
            "initial_qty": 0,
            "bought_qty": 1,
            "buy_fill_price": 107_000,
            "buy_logged": True,
            "stock_name": "KODEX 200",
            "owner": "stale-owner",
            "lease_until": "2026-07-28T09:35:00",
        })
        with self.env():
            result = run_live_order_smoke_test(
                broker,
                now=datetime(2026, 7, 28, 10, 0),
                store=store,
                sleep_fn=lambda _: None,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(broker.buy_calls, 0)
        self.assertEqual(broker.sell_calls, 0)
        self.assertTrue(store.state["quantity_restored"])


if __name__ == "__main__":
    unittest.main()
