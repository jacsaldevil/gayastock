"""Gemini 기반 주식 트레이딩 에이전트 (Vertex AI)"""
import json
import logging
import os
from datetime import datetime, time as dtime
from pathlib import Path
from data.utils import get_now_kst
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig, Part, Tool, grounding
from agent.tools import GEMINI_TOOLS, execute_tool, _broker, set_sim_portfolio, get_sim_portfolio
from data.trade_log import log_agent_run
from config import GCP_PROJECT_ID, GCP_REGION, GEMINI_MODEL, MAX_POSITIONS, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_HOLD_DAYS

logger = logging.getLogger(__name__)

vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)

_PROMPT_FILE = Path(__file__).parent.parent / "prompts" / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8").format(
    MAX_POSITIONS=MAX_POSITIONS,
    TAKE_PROFIT_PCT=TAKE_PROFIT_PCT,
    STOP_LOSS_PCT=STOP_LOSS_PCT,
    MAX_HOLD_DAYS=MAX_HOLD_DAYS,
)

MAX_TOOL_ROUNDS = 50

_SCHEDULE_SLOTS = [
    (dtime(9,  0),  "【1회차 — 장 시작】포트폴리오 점검 + 시장 레짐 확인 + 주도주 눌림목/과매도 반등 후보 스캔."),
    (dtime(9,  30), "【2회차 — 오전 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(10, 0),  "【3회차 — 오전 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(10, 30), "【4회차 — 오전 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(11, 0),  "【5회차 — 오전 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(11, 30), "【6회차 — 점심 전 스캔】포트폴리오 TP/SL/보유기간 확인. 신규 신호 탐색."),
    (dtime(12, 0),  "【7회차 — 오후 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(12, 30), "【8회차 — 오후 스캔】포트폴리오 TP/SL/보유기간 확인. 신규 신호 탐색."),
    (dtime(13, 0),  "【9회차 — 오후 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(13, 30), "【10회차 — 오후 스캔】포트폴리오 TP/SL/보유기간 확인. 신규 신호 탐색."),
    (dtime(14, 0),  "【11회차 — 오후 스캔】포트폴리오 TP/SL/보유기간 확인. 주도주 눌림목 + 과매도 반등 신호 탐색."),
    (dtime(14, 30), "【12회차 — 오후 스캔】포트폴리오 TP/SL/보유기간 확인. 신규 신호 탐색."),
    (dtime(15, 0),  "【13회차 — 마감 전】포트폴리오 TP/SL/보유기간 확인. 당일 마지막 신규 진입 기회."),
    (dtime(15, 30), "【14회차 — 장 마감】포트폴리오 최종 확인. 손절 기준 미달 종목 즉시 처리. 신규 매수 금지."),
]


def _get_run_slot(now: datetime) -> tuple[int, str]:
    mins = now.hour * 60 + now.minute
    indexed_slots = list(enumerate(_SCHEDULE_SLOTS))
    best_index, best = min(
        indexed_slots,
        key=lambda s: abs(s[1][0].hour * 60 + s[1][0].minute - mins),
    )
    return best_index, best[1]


def _get_run_context(now: datetime) -> str:
    return _get_run_slot(now)[1]


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
        watchlist: list[str] = None,
        sim_datetime: datetime | None = None,
        sim_portfolio_in: dict | None = None,
        cancel_pending: bool = True,
        skip_log: bool = False,
        on_tool_call: callable = None,
    ) -> str:
        self.tool_call_log = []
        self.buy_tickers: list[str] = []
        self.sim_portfolio_out: dict | None = None
        set_sim_portfolio(sim_portfolio_in)

        from agent.tools import load_stopped_out_today
        load_stopped_out_today()

        try:
            # 실전 모드 + 첫 루프에서만 미체결 주문 전량 취소
            if cancel_pending and os.environ.get("DRY_RUN", "false").lower() != "true":
                try:
                    cancelled = _broker().cancel_all_pending_orders()
                    if cancelled:
                        logger.info("미체결 주문 %d건 취소: %s", len(cancelled),
                                    [(c["ticker"], c["order_no"], c["success"]) for c in cancelled])
                        from data.trade_log import log_cancel
                        for c in cancelled:
                            if c.get("success") and c.get("action") == "BUY" and c.get("quantity", 0) > 0:
                                log_cancel(
                                    ticker=c["ticker"],
                                    quantity=c["quantity"],
                                    price=c.get("price", 0),
                                    order_no=c["order_no"],
                                    name=c.get("name", ""),
                                )
                                logger.info("미체결 BUY 취소 기록: %s %d주", c["ticker"], c["quantity"])
                except Exception as e:
                    logger.warning("미체결 주문 취소 중 오류 (무시하고 계속): %s", e)

            now = sim_datetime or get_now_kst()
            today = now.strftime("%Y-%m-%d %H:%M")
            slot_index, run_ctx = _get_run_slot(now)

            model = self._model_with_search if slot_index == 0 else self._model_no_search
            search_note = "Google Search grounding 사용" if slot_index == 0 else "검색 없이 실행"
            watchlist_note = ""
            scan_instruction = (
                "7. 시장 레짐이 허용할 때만 get_top_volume_stocks(n=50) → 주도 업종/거래대금 후보 선별 → get_technical_indicators()로 과매도 반등형 또는 주도주 눌림목형 진입 여부를 결정하세요.\n"
            )
            if watchlist:
                tickers = ", ".join(watchlist)
                watchlist_note = f"\n지정 분석 종목: {tickers}\n"
                scan_instruction = (
                    f"7. 시장 레짐이 허용할 때만 사용자가 지정한 종목({tickers})만 get_technical_indicators()로 "
                    "과매도 반등형 또는 주도주 눌림목형 진입 여부를 확인하세요. 지정 종목 외 신규 후보 스캔은 하지 마세요.\n"
                )

            user_message = (
                f"[{today}] {run_ctx}\n"
                f"{search_note}\n"
                "중기 트레이딩 전략 — 시장 레짐 확인 후 과매도 반등형 또는 주도주 눌림목형 기준으로 매매를 결정합니다.\n\n"
                f"{watchlist_note}"
                "1. get_market_regime()으로 시장 레짐을 확인하세요. buy_allowed=false이면 신규 매수는 금지입니다.\n"
                "2. get_portfolio()로 현재 포트폴리오를 확인하세요.\n"
                f"3. 수익률 +{TAKE_PROFIT_PCT}% 이상인 종목은 30~50% 부분익절하고 잔여는 추세 유지 시 보유하세요.\n"
                f"4. 수익률 -{STOP_LOSS_PCT}% 이하인 종목은 즉시 전량 매도(손절)하세요.\n"
                f"5. 보유기간 {MAX_HOLD_DAYS}영업일 초과 종목은 주봉 추세/BB/RSI를 재점검해 추세 둔화 시 청산하세요.\n"
                "6. 보유 종목의 get_technical_indicators()로 BB 상단 근접(upper_touch/above_upper) 또는 RSI≥70인 경우 부분/전량 매도하세요.\n"
                f"{scan_instruction}"
                "8. 분석 결과를 간결하게 요약하세요."
            )
            logger.info("에이전트 시작")
            try:
                chat = model.start_chat(response_validation=False)
                response = chat.send_message(user_message)
            except Exception as e:
                error_msg = f"Gemini API 초기 호출 실패: {e}"
                logger.error(error_msg)
                if not skip_log:
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
                        try:
                            parsed_result = json.loads(result_str)
                            if isinstance(parsed_result, dict):
                                result_success = parsed_result.get("success")
                            elif isinstance(parsed_result, list):
                                result_success = True
                            else:
                                result_success = None
                        except json.JSONDecodeError:
                            result_success = None
                        self.tool_call_log.append({
                            "round": i + 1,
                            "tool": fn.name,
                            "args": args,
                            "success": result_success,
                            "result_preview": result_str[:400],
                        })
                        if on_tool_call:
                            try:
                                on_tool_call(list(self.tool_call_log))
                            except Exception:
                                pass
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
                    if not skip_log:
                        log_agent_run(watchlist, error_msg, {})
                    return error_msg

            try:
                final = response.text or "분석 완료 (텍스트 응답 없음)"
            except Exception:
                # 마지막 응답이 function_call 파트인 경우 (MAX_TOOL_ROUNDS 도달)
                final = "분석 완료 (도구 호출 한도 도달 — 최종 텍스트 응답 없음)"
            logger.info("에이전트 완료")

            successful_buys: list[str] = []
            for tool_call in self.tool_call_log:
                if tool_call["tool"] != "buy_stock" or not tool_call["args"].get("ticker"):
                    continue
                if tool_call.get("success") is True:
                    successful_buys.append(tool_call["args"]["ticker"])
            self.buy_tickers = list(dict.fromkeys(successful_buys))

            if not skip_log:
                try:
                    portfolio_snapshot = _broker().get_balance()
                except Exception:
                    portfolio_snapshot = {}
                log_agent_run(watchlist, final, portfolio_snapshot, buy_tickers=self.buy_tickers)
            return final

        finally:
            self.sim_portfolio_out = get_sim_portfolio()
            set_sim_portfolio(None)

    def summarize_session(self, session_log: list[dict], sim_portfolio: dict | None = None) -> str:
        """루프 결과를 바탕으로 세션 최종 요약 생성 (LLM 1회 호출)."""
        parts = []
        for entry in session_log:
            loop_num = entry["loop"]
            result = entry.get("result") or "신호 없음 — 스킵"
            parts.append(f"【루프 {loop_num}】\n{result[:600]}")

        session_text = "\n\n".join(parts)
        prompt = (
            f"다음은 이번 트레이딩 세션(총 {len(session_log)}회 루프)의 실행 결과입니다:\n\n"
            f"{session_text}\n\n"
            "1. get_portfolio()로 최종 포트폴리오 상태를 확인하세요.\n"
            "2. 이번 세션의 매매 내역(매수/매도 종목, 이유)을 정리하세요.\n"
            "3. 현재 보유 종목의 BB 위치, RSI 상태를 간략히 평가하세요.\n"
            "4. 다음 세션에서 주의할 점을 한 줄로 작성하세요."
        )
        set_sim_portfolio(sim_portfolio)
        try:
            chat = self._model_no_search.start_chat(response_validation=False)
            response = chat.send_message(prompt)
            for _ in range(10):
                rparts = response.candidates[0].content.parts if response.candidates else []
                fn_calls = [p for p in rparts if p.function_call is not None]
                if not fn_calls:
                    break
                fn_responses = []
                for part in fn_calls:
                    fn = part.function_call
                    result_str = execute_tool(fn.name, dict(fn.args))
                    logger.info("요약 Tool: %s", fn.name)
                    fn_responses.append(
                        Part.from_function_response(
                            name=fn.name,
                            response={"result": result_str},
                        )
                    )
                response = chat.send_message(fn_responses)
            return response.text or "세션 요약 완료 (텍스트 없음)"
        except Exception as e:
            logger.error("세션 요약 LLM 호출 실패: %s", e)
            return "세션 요약 실패: " + str(e)
        finally:
            set_sim_portfolio(None)
