import unittest

from strategy.rules import classify_entry, evaluate_market_regime


def _candles(closes, latest_change=0.0):
    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "date": f"2026{(index // 28) + 1:02d}{(index % 28) + 1:02d}",
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 100_000,
            "change_rate": latest_change if index == len(closes) - 1 else 0.0,
        })
    return rows


class StrategyHardeningTest(unittest.TestCase):
    def test_volatility_overlap_uses_volatile_rebound_scale(self):
        base = [140 - index * 0.3 for index in range(60)]
        recent = [103 + ((-1) ** index) * 2.5 + index * 0.2 for index in range(19)] + [120]
        regime = evaluate_market_regime(_candles(base + recent, latest_change=4.0))

        self.assertGreaterEqual(regime["realized_vol_20d_pct"], 5.5)
        self.assertLess(regime["realized_vol_20d_pct"], 6.0)
        self.assertEqual(regime["status"], "volatile_rebound")
        self.assertEqual(regime["recommended_buy_scale"], 0.5)

    def test_eight_percent_cap_applies_to_oversold_reversal(self):
        regime = {
            "status": "volatile_rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.4,
        }
        technicals = {
            "bb_position": "lower_touch",
            "rsi": 35,
            "weekly_trend": "sideways",
            "change_rate": 9.0,
            "return_5d_pct": -4.0,
            "return_20d_pct": -10.0,
            "breakout_pct": -12.0,
            "volume_pace_ratio": 1.5,
            "atr14_pct": 6.0,
            "above_ma5": True,
            "above_ma20": False,
        }
        entry = classify_entry(regime, technicals)

        self.assertFalse(entry["allowed"])
        self.assertIn("+8% 초과", entry["reason"])

    def test_exactly_eight_percent_can_still_be_evaluated(self):
        regime = {
            "status": "volatile_rebound",
            "buy_allowed": True,
            "recommended_buy_scale": 0.4,
        }
        technicals = {
            "bb_position": "middle",
            "rsi": 60,
            "weekly_trend": "up",
            "change_rate": 8.0,
            "return_5d_pct": 3.0,
            "return_20d_pct": 2.0,
            "breakout_pct": -1.0,
            "volume_pace_ratio": 1.4,
            "atr14_pct": 6.0,
            "above_ma5": True,
            "above_ma20": True,
        }
        entry = classify_entry(regime, technicals)

        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["setup"], "volatile_rebound_leader")


if __name__ == "__main__":
    unittest.main()
