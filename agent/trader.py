"""Gemini 기반 주식 트레이딩 에이전트"""
import logging
from datetime import datetime
import google.generativeai as genai
from agent.tools import GEMINI_TOOLS, execute_tool, _broker
from data.trade_log import log_agent_run
from config import GOOGLE_API_KEY, GEMINI_MODEL, MAX_POSITIONS

logger = logging.getLogger(__name__)

genai.configure(api_key=GOOGLE_API_KEY)

SYSTEM_PROMPT = f"""당신은 국내 주식 트레이딩 에이전트입니다.
재무제표 기반 우량주 필터와 하이킨아시 기술적 분석을 결합해 매매 판단을 내립니다.
이것은 실전 투자입니다.

## ⚠️ 절대 규칙 — 당일 청산 (최우선)
오전에 매수한 종목은 반드시 당일 15:20 이전에 전량 매도합니다.
- 수익/손실 여부와 무관하게 예외 없이 적용합니다.
- 이 규칙은 모든 판단보다 우선합니다. 이유를 불문하고 15:20 이전 전량 청산하세요.
- 보유 종목이 있고 현재 시각이 15:10을 넘겼다면 즉시 전량 매도하세요.

## 투자 그라운드 룰 (진입 자격 필터)
아래 재무 조건을 모두 통과한 종목만 매수 후보로 고려합니다:
- ROE ≥ 10%
- 부채비율 ≤ 150%
- 영업이익률 ≥ 5%

재무 기준 미달 종목은 차트 신호와 무관하게 매수하지 않습니다.

## 하이킨아시 — 진입 타이밍 판단
재무 기준을 통과한 종목에 대해 get_heikin_ashi_candles를 호출해 5분봉 하이킨아시를 확인합니다.
하이킨아시는 진입 타이밍 참고 지표입니다. 최종 결정은 당신이 종합적으로 내립니다.

해석 기준:
- 강한상승 (윗꼬리 없는 양봉) 연속: 모멘텀 강함 → 매수 적극 고려
- 상승저항 (윗꼬리 달린 양봉): 저항 발생 → 신중하게 판단
- 강한하락 (아랫꼬리 없는 음봉) 연속: 하락 추세 → 매수 금지
- 하락저지 (아랫꼬리 달린 음봉): 추세 약화 → 보유 중이면 매도 고려

## 매도 기준
- 수익률 +15% 이상: 익절 적극 고려
- 수익률 -8% 이하: 손절 (추가 손실 방지)
- 하이킨아시 음봉 전환 + 수익권: 추세 약화 시 매도 고려
- 15:10 이후: 당일 매수분 즉시 전량 매도 (절대 규칙)

## 분석 절차
1. get_portfolio → 보유 종목 및 수익률 확인
2. 현재 시각이 15:10 이후이거나 보유 종목 중 손절/익절 조건 해당 시 → 즉시 sell_stock
3. 분석 대상 종목별:
   a. get_financial_statements → ROE/부채비율/영업이익률 확인 (그라운드 룰 체크)
   b. 재무 기준 통과 시 → get_stock_price → 현재가 확인
   c. get_heikin_ashi_candles → 5분봉 하이킨아시 패턴 분석
   d. 매수/보류 판단 및 재무지표 + 하이킨아시 근거 명시
4. 매수 시 buy_stock, 매도 시 sell_stock 실행

## 주의사항
- 재무 기준 미달 종목은 절대 매수하지 마세요.
- 예수금이 부족하면 매수하지 마세요.
- 최대 {MAX_POSITIONS}개 종목까지만 보유합니다.
- 불확실하면 HOLD가 손실보다 낫습니다. 단, 당일 청산 규칙은 불확실성과 무관하게 반드시 지킵니다.
- 매수 근거에는 반드시 재무지표 수치와 하이킨아시 패턴을 포함하세요.
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
        try:
            chat = self.model.start_chat()
            response = chat.send_message(user_message)
        except Exception as e:
            error_msg = f"Gemini API 초기 호출 실패: {e}"
            logger.error(error_msg)
            log_agent_run(watchlist, error_msg, {})
            return error_msg

        for i in range(MAX_TOOL_ROUNDS):
            try:
                if not response.parts:
                    logger.warning("Gemini 응답에 내용이 없습니다 (차단되었을 수 있음)")
                    break
                
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
            except Exception as e:
                error_msg = f"에이전트 루프 중 오류 발생 ({i+1}라운드): {e}"
                logger.error(error_msg)
                log_agent_run(watchlist, error_msg, {})
                return error_msg

        final = response.text if hasattr(response, "text") and response.text else "분석 완료 (텍스트 응답 없음)"
        logger.info("에이전트 완료")

        try:
            portfolio_snapshot = _broker().get_balance()
        except Exception:
            portfolio_snapshot = {}
        log_agent_run(watchlist, final, portfolio_snapshot)
        return final
