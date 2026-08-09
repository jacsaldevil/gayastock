import unittest
from datetime import datetime

from strategy.rules import (
    augment_technicals,
    calculate_position_size,
    classify_entry,
    evaluate_market_regime,
)


def candles_from_closes(closes, latest_change=0.0, volumes=None):
    volumes = volumes or [100_000] * len(closes)
    rows = []
    for i, close in enumerate(closes):
        rows.append({
            "date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}",
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": volumes[i],
            "change_rate": latest_change if i == len(closes) - 1 else 0.0,
        })
    return rows


def recovering_bear_market_closes():
    return [200 - i * 1.0 for i in range(70)] + [130, 131, 132, 133, 134, 136, 138, 140, 142, 145]


def volatile_rebound_closes():
    return [140 - i * 0.4 for i in range(60)] + [
        115, 108, 112, 104, 110, 102, 109, 101, 108, 100,
        107, 99, 106, 98, 100, 93, 96, 98, 102, 108,
    ]


def extreme_volatility_closes():
    return [140 - i * 0.4 for i in range(60)] + [
        115, 100, 114, 98, 112, 96, 110, 94, 108, 92,
        106, 90, 104, 88, 100, 90, 95, 98, 103, 108,
    ]


def recent_shock_closes():
    return [150.0] * 60 + [
        150, 135, 150, 134, 149, 133, 148, 132, 147, 131,
        146, 130, 145, 129, 142, 138, 132, 126, 120, 115,
    ]


class StrategyRulesTest(unittest.TestCase):
    def test_crash_blocks_new_buys(self):
        closes = [100 + i * 0.5 for i in range(79)] + [90]
        regime = evaluate_market_regime(candles_from_closes(closes, latest_change=-8.0))
        self.assertEqual(regime["status"], "crash")
        self.assertFalse(regime["buy_allowed"])

    def test_rebound_below_ma60_allows_reduced_entries(self):
        regime = evaluate_market_regime(
            candles_from_closes(recovering_bear_market_closes(), latest_change=2.5)
        )
        self.assertEqual(regime["status"], "rebound")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.7)

    def test_volatile_rebound_allows_small_entries(self):
        regime = evaluate_market_regime(
            candles_from_closes(volatile_rebound_closes(), latest_change=8.0)
        )
        self.assertEqual(regime["status"], "volatile_rebound")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.5)
        self.assertGreaterEqual(regime["realized_vol_20d_pct"], 5.5)
        self.assertLessEqual(regime["realized_vol_20d_pct"], 8.0)

    def test_extreme_volatility_without_same_day_crash_keeps_scanning(self):
        regime = evaluate_market_regime(
            candles_from_closes(extreme_volatility_closes(), latest_change=8.0)
        )
        self.assertEqual(regime["status"], "risk_off_selective")
        self.assertTrue(regime["buy_allowed"])
        names = [item["name"] for item in regime["failed_conditions"]]
        self.assertIn("realized_vol_20d_pct", names)

    def test_mild_recovery_uses_selective_risk_off(self):
        regime = evaluate_market_regime(
            candles_from_closes(recovering_bear_market_closes(), latest_change=0.6)
        )
        self.assertEqual(regime["status"], "risk_off_selective")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.5)

    def test_negative_risk_off_keeps_selective_entries_open(self):
        regime = evaluate_market_regime(
            candles_from_closes(recovering_bear_market_closes(), latest_change=-1.0)
        )
        self.assertEqual(regime["status"], "risk_off_selective")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.4)

    def test_recent_shock_is_selective_instead_of_hard_blocked(self):
        regime = evaluate_market_regime(
            candles_from_closes(recent_shock_closes(), latest_change=-0.8)
        )
        self.assertEqual(regime["status"], "crash_selective")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.35)
        self.assertLessEqual(regime["return_5d_pct"], -6.0)
        self.assertGreaterEqual(regime["realized_vol_20d_pct"], 5.0)

    def test_momentum_breakout_allows_upper_band(self):
        regime = {"status": "risk_on", "buy_allowed": True, "recommended_buy_scale": 1.0}
        tech = {
            "bb_position": "upper_touch",
            "rsi": 66,
            "weekly_trend": "up",
            "change_rate": 3.0,
            "return_5d_pct": 5.0,
            "return_20d_pct": 15.0,
            "breakout_pct": 1.2,
            "volume_pace_ratio": 1.8,
            "atr14_pct": 4.0,
            "above_ma5": True,
            "above_ma20": True,
        }
        entry = classify_entry(regime, tech)
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["setup"], "momentum_breakout")

    def test_upper_band_without_breakout_is_rejected(self):
        regime = {"status": "risk_on", "buy_allowed": True, "recommended_buy_scale": 1.0}
        tech = {
            "bb_position": "upper_touch",
            "rsi": 66,
            "weekly_trend": "up",
            "change_rate": 3.0,
            "return_5d_pct": 5.0,
            "return_20d_pct": 15.0,
            "breakout_pct": -1.0,
            "volume_pace_ratio": 1.8,
            "atr14_pct": 4.0,
            "above_ma5": True,
            "above_ma20": True,
        }
        self.assertFalse(classify_entry(regime, tech)["allowed"])

    def test_volatile_rebound_leader_is_allowed(self):
        regime = {
            "status": "volatile_rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.4,
        }
        tech = {
            "bb_position": "middle",
            "rsi": 61,
            "weekly_trend": "up",
            "change_rate": 4.5,
            "return_5d_pct": 3.0,
            "return_20d_pct": 2.0,
            "breakout_pct": -1.5,
            "volume_pace_ratio": 1.4,
            "atr14_pct": 6.0,
            "above_ma5": True,
            "above_ma20": True,
        }
        entry = classify_entry(regime, tech)
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["setup"], "volatile_rebound_leader")
        self.assertEqual(entry["setup_scale"], 0.8)

    def test_volatile_rebound_does_not_chase_stock_above_eight_percent(self):
        regime = {
            "status": "volatile_rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.4,
        }
        tech = {
            "bb_position": "upper_touch",
            "rsi": 64,
            "weekly_trend": "up",
            "change_rate": 9.0,
            "return_5d_pct": 5.0,
            "return_20d_pct": 8.0,
            "breakout_pct": 1.0,
            "volume_pace_ratio": 1.8,
            "atr14_pct": 6.0,
            "above_ma5": True,
            "above_ma20": True,
        }
        self.assertFalse(classify_entry(regime, tech)["allowed"])

    def test_selective_risk_off_allows_only_oversold_reversal(self):
        regime = {
            "status": "risk_off_selective",
            "buy_allowed": True,
            "recommended_buy_scale": 0.35,
        }
        tech = {
            "bb_position": "lower_touch",
            "rsi": 36,
            "weekly_trend": "sideways",
            "change_rate": 1.2,
            "return_5d_pct": -3.0,
            "return_20d_pct": -12.0,
            "breakout_pct": -15.0,
            "volume_pace_ratio": 1.1,
            "atr14_pct": 5.0,
            "above_ma5": True,
            "above_ma20": False,
        }
        entry = classify_entry(regime, tech)
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["setup"], "oversold_reversal")
        self.assertEqual(entry["setup_scale"], 0.8)

    def test_crash_selective_allows_relative_strength_recovery(self):
        regime = {
            "status": "crash_selective",
            "buy_allowed": True,
            "recommended_buy_scale": 0.35,
        }
        tech = {
            "bb_position": "middle",
            "rsi": 55,
            "weekly_trend": "sideways",
            "change_rate": 1.2,
            "return_5d_pct": -1.0,
            "return_20d_pct": -4.0,
            "breakout_pct": -7.0,
            "volume_pace_ratio": 1.2,
            "atr14_pct": 6.0,
            "above_ma5": True,
            "above_ma20": False,
        }
        entry = classify_entry(regime, tech)
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["setup"], "relative_strength_recovery")

    def test_crash_selective_scale_can_buy_one_moderate_price_share(self):
        sized = calculate_position_size(
            price=30_000,
            cash=283_606,
            total_eval=283_606,
            holdings_eval=0,
            atr_pct=6.0,
            regime_scale=0.35,
            setup_scale=0.8,
            max_buy_amount=500_000,
        )
        self.assertEqual(sized["quantity"], 1)

    def test_position_size_is_risk_capped(self):
        sized = calculate_position_size(
            price=10_000,
            cash=300_000,
            total_eval=300_000,
            holdings_eval=0,
            atr_pct=4.0,
            regime_scale=1.0,
            setup_scale=1.0,
            max_buy_amount=500_000,
        )
        self.assertGreater(sized["quantity"], 0)
        self.assertLessEqual(sized["quantity"] * 10_000, 120_000)

    def test_rebound_scale_can_buy_one_moderate_price_share(self):
        sized = calculate_position_size(
            price=30_000,
            cash=283_616,
            total_eval=283_616,
            holdings_eval=0,
            atr_pct=5.0,
            regime_scale=0.6,
            setup_scale=0.8,
            max_buy_amount=500_000,
        )
        self.assertGreaterEqual(sized["quantity"], 1)
        self.assertLessEqual(sized["max_amount"], 55_000)

    def test_volatile_rebound_scale_can_buy_one_moderate_price_share(self):
        sized = calculate_position_size(
            price=30_000,
            cash=283_606,
            total_eval=283_606,
            holdings_eval=0,
            atr_pct=6.0,
            regime_scale=0.4,
            setup_scale=0.8,
            max_buy_amount=500_000,
        )
        self.assertEqual(sized["quantity"], 1)
        self.assertLessEqual(sized["max_amount"], 37_000)

    def test_selective_risk_off_can_buy_one_lower_price_share(self):
        sized = calculate_position_size(
            price=30_000,
            cash=283_616,
            total_eval=283_616,
            holdings_eval=0,
            atr_pct=5.0,
            regime_scale=0.35,
            setup_scale=0.8,
            max_buy_amount=500_000,
        )
        self.assertEqual(sized["quantity"], 1)
        self.assertLessEqual(sized["max_amount"], 32_000)

    def test_existing_position_reduces_additional_size(self):
        sized = calculate_position_size(
            price=10_000,
            cash=300_000,
            total_eval=300_000,
            holdings_eval=100_000,
            atr_pct=4.0,
            regime_scale=1.0,
            setup_scale=1.0,
            max_buy_amount=500_000,
            existing_position_value=160_000,
        )
        self.assertEqual(sized["quantity"], 0)

    def test_augment_technicals_computes_breakout_and_atr(self):
        closes = [100 + i for i in range(60)]
        rows = candles_from_closes(closes, latest_change=2.0, volumes=[100_000] * 59 + [200_000])
        tech = augment_technicals(
            {"bb_position": "upper_touch", "rsi": 65, "weekly_trend": "up"},
            rows,
            current_price=161,
            now=datetime(2026, 7, 10, 15, 0),
        )
        self.assertIn("atr14_pct", tech)
        self.assertGreater(tech["breakout_pct"], 0)


if __name__ == "__main__":
    unittest.main()
