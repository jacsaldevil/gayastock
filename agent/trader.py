"""Gemini 기반 주식 트레이딩 에이전트"""
import logging
from datetime import datetime
from pathlib import Path
import google.generativeai as genai
from agent.tools import GEMINI_TOOLS, execute_tool, _broker
from data.trade_log import log_agent_run
from config import GOOGLE_API_KEY, GEMINI_MODEL, MAX_POSITIONS

logger = logging.getLogger(__name__)

genai.configure(api_key=GOOGLE_API_KEY)

_PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8").format(MAX_POSITIONS=MAX_POSITIONS)

MAX_TOOL_ROUNDS = 30


class TradingAgent:
    def __init__(self):
        self.tool_call_log: list[dict] = []
        self.model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            tools=[GEMINI_TOOLS],
            system_instruction=SYSTEM_PROMPT,
            generation_config=genai.GenerationConfig(temperature=0.1),
        )

    def run(self, watchlist: list[str], sim_datetime: datetime | None = None) -> str:
        self.tool_call_log = []
        now = sim_datetime or datetime.now()
        today = now.strftime("%Y-%m-%d %H:%M")
        user_message = (
            f"[{today}] 트레이딩을 시작합니다.\n"
            f"기본 분석 종목(워치리스트): {', '.join(watchlist)}\n\n"
            "1. 현재 포트폴리오를 확인하세요.\n"
            "2. 워치리스트 종목의 현재가와 재무제표를 분석하세요.\n"
            "3. 필요시 get_top_volume_stocks로 시장 관심 종목을 추가 발굴하세요 (발굴 종목은 더 엄격한 기준 적용).\n"
            "4. 매수/매도/보유 판단을 내리고 필요시 주문을 실행하세요.\n"
            "5. 분석 결과와 판단 근거를 요약해 주세요."
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
                    args = dict(fn.args)
                    logger.info(f"Tool: {fn.name} | {args}")
                    result_str = execute_tool(fn.name, args)
                    logger.info(f"결과: {result_str[:200]}")
                    self.tool_call_log.append({
                        "round": i + 1,
                        "tool": fn.name,
                        "args": args,
                        "result_preview": result_str[:400],
                    })
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
