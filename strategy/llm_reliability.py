"""Runtime reliability helpers for transient Gemini resource exhaustion."""
from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Callable, Iterable

logger = logging.getLogger(__name__)
_DEFAULT_DELAYS = (5, 15)
_POLICY_NOTE = """

## 운영 보완 — 약세장 선별 진입

당일 시장이 -5% 이하로 폭락한 경우만 신규 매수를 전면 금지합니다.
최근 5일 급락·고변동성 또는 60일선 하회 상태라도 `buy_allowed=true`이면 반드시
거래대금 후보를 탐색하고, 과매도 반전·상대강도 회복 종목을 소규모로 검토합니다.
`crash_selective`는 정상 규모의 35%, `risk_off_selective`는 40~50%만 허용합니다.

시장보다 먼저 MA5와 거래량을 회복한 종목은 `relative_strength_recovery`로 진입할 수 있습니다.
당일 +8% 초과 추격 금지, 09:10~15:20 매수 시간, 손절·수량 제한은 그대로 유지합니다.
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

    if "## 운영 보완 — 약세장 선별 진입" not in trader.SYSTEM_PROMPT:
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
