"""Claude 기반 주식 트레이딩 에이전트"""
import json
import logging
from datetime import datetime
import anthropic
from agent.tools import TOOLS, execute_tool, broker
from data.trade_log import log_agent_run
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_POSITIONS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""당신은 국내 주식 트레이딩 에이전트입니다. 한국 주식 시장(KOSPI/KOSDAQ)에서 재무제표 기반으로 투자 판단을 내립니다.

## 투자 원칙
- **가치투자 중심**: 재무제표(ROE, 부채비율, 영업이익률, PER, PBR)를 분석하여 저평가된 우량주를 발굴합니다.
- **매수 기준**: ROE ≥ 10%, 부채비율 ≤ 150%, 영업이익률 ≥ 5%, PER이 업종 평균 이하
- **매도 기준**: 목표 수익률 +15% 달성, 또는 손실 -8% 초과 시 손절
- **분산 투자**: 최대 {MAX_POSITIONS}개 종목 보유, 한 종목에 집중 금지
- **모의투자 중**: 실제 돈이 아니므로 학습 목적으로 적극적으로 판단하되, 실제와 동일한 기준을 적용합니다.

## 분석 절차
1. get_portfolio → 현재 잔고/보유 종목 확인
2. 분석할 종목의 get_stock_price → 현재가 및 PER/PBR 확인
3. get_financial_statements → 연간 재무제표로 수익성/안정성 검토
4. 매수/매도/보유 결정 후 근거를 명확히 설명
5. 매수 시 buy_stock, 매도 시 sell_stock 호출

## 주의사항
- 종목 분석 없이 바로 매매하지 마세요.
- 매수 근거는 반드시 재무지표 수치를 포함해야 합니다.
- 예수금이 부족하면 매수하지 마세요.
"""


class TradingAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    def run(self, watchlist: list[str]) -> str:
        """
        watchlist: 분석할 종목코드 리스트 (예: ["005930", "000660"])
        에이전트가 자율적으로 재무 분석 후 매매 결정을 내립니다.
        """
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        user_message = (
            f"[{today}] 오늘의 트레이딩을 시작합니다.\n"
            f"분석 대상 종목: {', '.join(watchlist)}\n\n"
            "1. 현재 포트폴리오를 확인하세요.\n"
            "2. 각 종목의 현재가와 재무제표를 분석하세요.\n"
            "3. 매수/매도/보유 판단을 내리고 필요시 주문을 실행하세요.\n"
            "4. 분석 결과와 판단 근거를 요약해 주세요."
        )

        messages = [{"role": "user", "content": user_message}]
        logger.info(f"트레이딩 에이전트 시작: {watchlist}")

        # tool_use 루프
        while True:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # assistant 응답을 messages에 추가
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                final = next(
                    (block.text for block in response.content if hasattr(block, "text")),
                    "분석 완료"
                )
                logger.info("에이전트 완료")
                try:
                    portfolio_snapshot = broker.get_balance()
                except Exception:
                    portfolio_snapshot = {}
                log_agent_run(watchlist, final, portfolio_snapshot)
                return final

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(f"Tool 호출: {block.name} | 입력: {block.input}")
                        result_str = execute_tool(block.name, block.input)
                        logger.info(f"Tool 결과: {result_str[:200]}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })

                messages.append({"role": "user", "content": tool_results})
                continue

            # 예상치 못한 stop_reason
            break

        return "에이전트 실행 종료"
