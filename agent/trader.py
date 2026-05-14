"""Gemini 기반 주식 트레이딩 에이전트 (Vertex AI)"""
import logging
import os
from datetime import datetime, time as dtime
from pathlib import Path
from data.utils import get_now_kst
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig, Part, Content, Tool, grounding
from agent.tools import GEMINI_TOOLS, execute_tool, _broker, set_sim_portfolio, get_sim_portfolio
from data.trade_log import log_agent_run
from config import GCP_PROJECT_ID, GCP_REGION, GEMINI_MODEL, MAX_POSITIONS, TAKE_PROFIT_PCT, STOP_LOSS_PCT

logger = logging.getLogger(__name__)

vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)

_PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8").format(
    MAX_POSITIONS=MAX_POSITIONS,
    TAKE_PROFIT_PCT=TAKE_PROFIT_PCT,
    STOP_LOSS_PCT=STOP_LOSS_PCT,
)

MAX_TOOL_ROUNDS = 30

_SCHEDULE_SLOTS = [
    (dtime(9, 20),  "【1회차 — 진입】장 시작 20분 경과. Google 검색 후 Top10 스캔, HA 신호 강한 종목 적극 매수."),
    (dtime(9, 31),  "【2회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(9, 42),  "【3회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(9, 53),  "【4회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(10, 4),  "【5회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(10, 15), "【6회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(10, 26), "【7회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(10, 37), "【8회차 — 오전 점검】TP/SL 먼저 확인. HA 강/중 진입 가능."),
    (dtime(10, 48), "【9회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(10, 59), "【10회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(11, 10), "【11회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(11, 21), "【12회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(11, 32), "【13회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(11, 43), "【14회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(11, 54), "【15회차 — 오전 후반 점검】TP/SL 확인. HA 강 우선, 중은 선택적 진입."),
    (dtime(12, 5),  "【16회차 — 정리 점검】TP/SL 확인. HA 강(강한상승 1봉+)만 허용. 신규 진입 자제."),
    (dtime(12, 16), "【17회차 — 정리 점검】TP/SL 확인. HA 강(강한상승 1봉+)만 허용. 신규 진입 자제."),
    (dtime(12, 27), "【18회차 — 정리 점검】TP/SL 확인. HA 강(강한상승 1봉+)만 허용. 신규 진입 자제."),
    (dtime(12, 38), "【19회차 — 청산 준비】TP/SL 확인. 신규 매수 금지. 수익 중인 종목도 청산 고려."),
    (dtime(13, 0),  "【20회차 — 강제 청산】신규 매수 절대 금지. 보유 전종목 즉시 전량 매도."),
]


def _get_run_context(now: datetime) -> str:
    mins = now.hour * 60 + now.minute
    best = min(_SCHEDULE_SLOTS, key=lambda s: abs(s[0].hour * 60 + s[0].minute - mins))
    return best[1]


def _make_search_tool():
    """Google Search grounding Tool 생성 — SDK 버전별 호환."""
    # 1) 신규 SDK: grounding.GoogleSearch()
    try:
        return Tool(google_search=grounding.GoogleSearch())
    except AttributeError:
        pass
    # 2) Tool 생성자에 직접 dict 전달 (중간 SDK 버전)
    try:
        return Tool(google_search={})
    except Exception:
        pass
    # 3) 포기 — 검색 없이 실행
    logger.warning("Google Search grounding 미지원 SDK — 검색 없이 실행")
    return None


class TradingAgent:
    def __init__(self):
        self.tool_call_log: list[dict] = []
        search_tool = _make_search_tool()
        search_tools = [GEMINI_TOOLS, search_tool] if search_tool else [GEMINI_TOOLS]
        self._model_with_search = GenerativeModel(
            model_name=GEMINI_MODEL,
            tools=search_tools,
            system_instruction=SYSTEM_PROMPT,
            generation_config=GenerationConfig(temperature=0.1),
        )
        self._model_no_search = GenerativeModel(
            model_name=GEMINI_MODEL,
            tools=[GEMINI_TOOLS],
            system_instruction=SYSTEM_PROMPT,
            generation_config=GenerationConfig(temperature=0.1),
        )

    def run(
        self,
        watchlist: list[str],
        sim_datetime: datetime | None = None,
        sim_portfolio_in: dict | None = None,
    ) -> str:
        self.tool_call_log = []
        self.sim_portfolio_out: dict | None = None
        set_sim_portfolio(sim_portfolio_in)

        try:
            # 실전 모드에서만 미체결 주문 전량 취소 후 시작
            if os.environ.get("DRY_RUN", "false").lower() != "true":
                try:
                    cancelled = _broker().cancel_all_pending_orders()
                    if cancelled:
                        logger.info("미체결 주문 %d건 취소: %s", len(cancelled),
                                    [(c["ticker"], c["order_no"], c["success"]) for c in cancelled])
                except Exception as e:
                    logger.warning("미체결 주문 취소 중 오류 (무시하고 계속): %s", e)

            now = sim_datetime or get_now_kst()
            today = now.strftime("%Y-%m-%d %H:%M")
            run_ctx = _get_run_context(now)

            # Google Search Grounding은 1회차(오전 진입)에만 사용
            is_first_run = "1회차" in run_ctx
            model = self._model_with_search if is_first_run else self._model_no_search
            user_message = (
                f"[{today}] {run_ctx}\n"
                "트레이딩을 시작합니다.\n\n"
                "1. 현재 포트폴리오를 확인하세요.\n"
                "2. TP/SL 조건 해당 종목을 즉시 처리하세요.\n"
                "3. get_top_volume_stocks(n=30)으로 거래량 Top30 스캔 후 ETF/스팩 제외, 상위 10종목 선정하세요.\n"
                "4. 각 종목의 현재가와 하이킨아시 패턴을 확인하고 매수/보류를 판단하세요.\n"
                "5. 분석 결과와 판단 근거를 최종 보고서 형식으로 작성하세요."
            )

            logger.info(f"트레이딩 에이전트 시작: {watchlist} (Google Search: {'ON' if is_first_run else 'OFF'})")
            try:
                chat = model.start_chat()
                response = chat.send_message(user_message)
            except Exception as e:
                error_msg = f"Gemini API 초기 호출 실패: {e}"
                logger.error(error_msg)
                log_agent_run(watchlist, error_msg, {})
                return error_msg

            for i in range(MAX_TOOL_ROUNDS):
                try:
                    parts = (
                        response.candidates[0].content.parts
                        if response.candidates else []
                    )
                    if not parts:
                        logger.warning("Gemini 응답에 내용이 없습니다 (차단되었을 수 있음)")
                        break

                    fn_calls = [p for p in parts if p.function_call is not None]

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
                            Part.from_function_response(
                                name=fn.name,
                                response={"result": result_str},
                            )
                        )

                    response = chat.send_message(fn_responses)
                except Exception as e:
                    error_msg = f"에이전트 루프 중 오류 발생 ({i+1}라운드): {e}"
                    logger.error(error_msg)
                    log_agent_run(watchlist, error_msg, {})
                    return error_msg

            try:
                final = response.text or "분석 완료 (텍스트 응답 없음)"
            except Exception:
                # 마지막 응답이 function_call 파트인 경우 (MAX_TOOL_ROUNDS 도달)
                final = "분석 완료 (도구 호출 한도 도달 — 최종 텍스트 응답 없음)"
            logger.info("에이전트 완료")

            try:
                portfolio_snapshot = _broker().get_balance()
            except Exception:
                portfolio_snapshot = {}
            log_agent_run(watchlist, final, portfolio_snapshot)
            return final

        finally:
            self.sim_portfolio_out = get_sim_portfolio()
            set_sim_portfolio(None)
