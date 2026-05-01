"""Gemini 기반 주식 트레이딩 에이전트"""
import logging
from datetime import datetime
import google.generativeai as genai
from agent.tools import GEMINI_TOOLS, execute_tool, broker
from data.trade_log import log_agent_run
from config import GOOGLE_API_KEY, GEMINI_MODEL, MAX_POSITIONS

logger = logging.getLogger(__name__)

genai.configure(api_key=GOOGLE_API_KEY)

SYSTEM_PROMPT = f"""당신은 국내 주식 트레이딩 에이전트입니다. 한국 주식 시장(KOSPI/KOSDAQ)에서 재무제표 기반으로 투자 판단을 내립니다.
이것은 실전 투자입니다. 신중하고 보수적으로 판단하세요.

## 투자 원칙
- **가치투자 중심**: 재무제표(ROE, 부채비율, 영업이익률, PER, PBR)를 분석하여 저평가된 우량주를 발굴합니다.
- **매수 기준**: ROE ≥ 10%, 부채비율 ≤ 150%, 영업이익률 ≥ 5%, PER이 업종 평균 이하
- **매도 기준**: 목표 수익률 +15% 달성, 또는 손실 -8% 초과 시 손절
- **분산 투자**: 최대 {MAX_POSITIONS}개 종목 보유, 한 종목에 집중 금지
- **보수적 판단**: 불확실하면 매수하지 마세요. 현금 보유가 손실보다 낫습니다.

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
- 확신이 없으면 보유(HOLD) 판단을 내리세요.
"""

MAX_TOOL_ROUNDS = 30


class TradingAgent:
    def __init__(self):
        self.model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            tools=[GEMINI_TOOLS],
            system_instruction=SYSTEM_PROMPT,
            generation_config=genai.GenerationConfig(temperature=0.1),
        )

    def run(self, watchlist: list[str]) -> str:
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        user_message = (
            f"[{today}] 트레이딩을 시작합니다.\n"
            f"분석 대상 종목: {', '.join(watchlist)}\n\n"
            "1. 현재 포트폴리오를 확인하세요.\n"
            "2. 각 종목의 현재가와 재무제표를 분석하세요.\n"
            "3. 매수/매도/보유 판단을 내리고 필요시 주문을 실행하세요.\n"
            "4. 분석 결과와 판단 근거를 요약해 주세요."
        )

        logger.info(f"트레이딩 에이전트 시작: {watchlist}")
        chat = self.model.start_chat()
        response = chat.send_message(user_message)

        for _ in range(MAX_TOOL_ROUNDS):
            fn_calls = [p for p in response.parts if hasattr(p, "function_call") and p.function_call.name]

            if not fn_calls:
                break

            fn_responses = []
            for part in fn_calls:
                fn = part.function_call
                logger.info(f"Tool: {fn.name} | {dict(fn.args)}")
                result_str = execute_tool(fn.name, dict(fn.args))
                logger.info(f"결과: {result_str[:200]}")
                fn_responses.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fn.name,
                            response={"result": result_str},
                        )
                    )
                )

            response = chat.send_message(fn_responses)

        final = response.text if hasattr(response, "text") and response.text else "분석 완료"
        logger.info("에이전트 완료")

        try:
            portfolio_snapshot = broker.get_balance()
        except Exception:
            portfolio_snapshot = {}
        log_agent_run(watchlist, final, portfolio_snapshot)
        return final
