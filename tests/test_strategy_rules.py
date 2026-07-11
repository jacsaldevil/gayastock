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


class StrategyRulesTest(unittest.TestCase):
    def test_crash_blocks_new_buys(self):
        closes = [100 + i * 0.5 for i in range(79)] + [90]
        regime = evaluate_market_regime(candles_from_closes(closes, latest_change=-8.0))
        self.assertEqual(regime["status"], "crash")
        self.assertFalse(regime["buy_allowed"])

    def test_rebound_below_ma60_is_small_scale(self):
        closes = [200 - i * 1.0 for i in range(70)] + [130, 131, 132, 133, 134, 136, 138, 140, 142, 145]
        regime = evaluate_market_regime(candles_from_closes(closes, latest_change=2.5))
        self.assertEqual(regime["status"], "rebound")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.25)

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
