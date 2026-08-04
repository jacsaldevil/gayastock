"""Runtime reliability helpers for transient Gemini resource exhaustion."""
from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Callable, Iterable

logger = logging.getLogger(__name__)
_DEFAULT_DELAYS = (5, 15)
_POLICY_NOTE = """

## 운영 보완 — 고변동성 완만 회복

기존 강한 V자 반등 외에도 다음 조건을 모두 충족하면 `volatile_rebound`로 분류합니다.

- 시장 프록시가 MA5보다 최소 2% 위
- 당일 상승률 +0.3%~+8%
- 5일 수익률 +1% 이상
- MA20 기울기 -8%보다 양호
- 20일 낙폭 -25% 이상
- 20일 실현변동성 5.5%~8%

이 완만 회복 경로의 시장 배율은 0.35입니다. 개별 종목은 기존
`volatile_rebound_leader` 또는 `oversold_reversal` 코드 검증을 그대로 통과해야 하며,
당일 +8% 초과 추격 금지와 09:10~15:20 매수 시간 제한을 유지합니다.
"""


def _is_initial_resource_exhausted(result: str) -> bool:
    text = str(result or "")
    return (
        text.startswith("Gemini API 초기 호출 실패:")
        and ("429" in text or "Resource exhausted" in text)
    )


def _is_summary_resource_exhausted(result: str) -> bool:
    text = str(result or "")
    return (
        text.startswith("세션 요약 실패:")
        and ("429" in text or "Resource exhausted" in text)
    )


def _retry_text_result(
    call: Callable[[], str],
    predicate: Callable[[str], bool],
    *,
    delays: Iterable[int] = _DEFAULT_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
    label: str = "Gemini",
) -> str:
    """Retry only explicitly classified transient text results."""
    result = call()
    for attempt, delay in enumerate(tuple(delays), start=1):
        if not predicate(result):
            break
        logger.warning("%s 429 감지 — %d초 후 재시도 (%d)", label, delay, attempt)
        sleep_fn(delay)
        result = call()
    return result


def install() -> None:
    """Patch TradingAgent before instances are created."""
    from agent import trader

    if getattr(trader, "_LLM_RELIABILITY_INSTALLED", False):
        return

    if "## 운영 보완 — 고변동성 완만 회복" not in trader.SYSTEM_PROMPT:
        trader.SYSTEM_PROMPT += _POLICY_NOTE

    original_run = trader.TradingAgent.run
    original_summarize = trader.TradingAgent.summarize_session

    @wraps(original_run)
    def run_with_retry(self, *args, **kwargs):
        retry_kwargs = dict(kwargs)
        first_call = True

        def call() -> str:
            nonlocal first_call
            current = dict(retry_kwargs)
            if not first_call:
                # 재시도에서 미체결 주문 취소를 반복하지 않는다.
                current["cancel_pending"] = False
            first_call = False
            return original_run(self, *args, **current)

        return _retry_text_result(
            call,
            _is_initial_resource_exhausted,
            label="Gemini 초기 호출",
        )

    @wraps(original_summarize)
    def summarize_with_retry(self, *args, **kwargs):
        return _retry_text_result(
            lambda: original_summarize(self, *args, **kwargs),
            _is_summary_resource_exhausted,
            label="Gemini 세션 요약",
        )

    trader.TradingAgent.run = run_with_retry
    trader.TradingAgent.summarize_session = summarize_with_retry
    trader._LLM_RELIABILITY_INSTALLED = True
