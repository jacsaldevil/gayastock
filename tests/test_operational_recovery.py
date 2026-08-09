import unittest

from strategy.llm_reliability import (
    _is_initial_resource_exhausted,
    _is_summary_resource_exhausted,
    _retry_text_result,
)
from strategy.recovery_override import evaluate_market_regime


def candles_from_closes(closes, latest_change=0.0):
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


def volatile_recovery_closes():
    return [140 - i * 0.4 for i in range(60)] + [
        115, 108, 112, 104, 110, 102, 109, 101, 108, 100,
        107, 99, 106, 98, 100, 93, 96, 98, 102, 108,
    ]


class OperationalRecoveryTest(unittest.TestCase):
    def test_steady_high_vol_recovery_opens_small_scale(self):
        regime = evaluate_market_regime(
            candles_from_closes(volatile_recovery_closes(), latest_change=0.7)
        )
        self.assertEqual(regime["status"], "volatile_rebound")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.5)
        self.assertIn("완만 회복", regime["reason"])

    def test_high_vol_without_positive_confirmation_stays_small_selective(self):
        regime = evaluate_market_regime(
            candles_from_closes(volatile_recovery_closes(), latest_change=-0.6)
        )
        self.assertEqual(regime["status"], "risk_off_selective")
        self.assertTrue(regime["buy_allowed"])
        self.assertEqual(regime["recommended_buy_scale"], 0.4)
        names = [item["name"] for item in regime.get("failed_conditions", [])]
        self.assertIn("change_rate_high_vol", names)
        self.assertNotIn("realized_vol_20d_pct", names)

    def test_initial_429_retries_then_returns_success(self):
        results = iter([
            "Gemini API 초기 호출 실패: 429 Resource exhausted",
            "정상 완료",
        ])
        sleeps = []
        result = _retry_text_result(
            lambda: next(results),
            _is_initial_resource_exhausted,
            delays=(1, 2),
            sleep_fn=sleeps.append,
            label="test",
        )
        self.assertEqual(result, "정상 완료")
        self.assertEqual(sleeps, [1])

    def test_non_initial_error_is_not_retried(self):
        calls = []

        def call():
            calls.append(1)
            return "에이전트 루프 중 오류 발생: 429 Resource exhausted"

        result = _retry_text_result(
            call,
            _is_initial_resource_exhausted,
            delays=(1, 2),
            sleep_fn=lambda _: None,
        )
        self.assertIn("429", result)
        self.assertEqual(len(calls), 1)

    def test_summary_429_is_retryable(self):
        self.assertTrue(_is_summary_resource_exhausted(
            "세션 요약 실패: 429 Resource exhausted"
        ))


if __name__ == "__main__":
    unittest.main()
