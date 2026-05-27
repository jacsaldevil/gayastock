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

MAX_TOOL_ROUNDS = 50

_SCHEDULE_SLOTS = [
    (dtime(9, 20),  "【1회차 — 진입】장 시작 20분 경과. Google 검색 후 Top10 스캔, VWAP +1~+2% 종목 적극 매수."),
    (dtime(9, 27),  "【2회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(9, 34),  "【3회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(9, 41),  "【4회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(9, 48),  "【5회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(9, 55),  "【6회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(10, 2),  "【7회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(10, 9),  "【8회차 — 오전 점검】TP/SL 먼저 확인. VWAP +1~+2% 진입 가능."),
    (dtime(10, 16), "【9회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(10, 23), "【10회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(10, 30), "【11회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(10, 37), "【12회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(10, 44), "【13회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(10, 51), "【14회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(10, 58), "【15회차 — 오전 후반 점검】TP/SL 확인. VWAP +1~+2% 조건 충족 시만 진입."),
    (dtime(11, 5),  "【16회차 — 정리 점검】TP/SL 확인. 신규 진입 자제. VWAP +1~+1.5% 이내만 허용."),
    (dtime(11, 12), "【17회차 — 정리 점검】TP/SL 확인. 신규 진입 자제. VWAP +1~+1.5% 이내만 허용."),
    (dtime(11, 19), "【18회차 — 정리 점검】TP/SL 확인. 신규 진입 자제. VWAP +1~+1.5% 이내만 허용."),
    (dtime(11, 26), "【19회차 — 청산 준비】TP/SL 확인. 신규 매수 금지. 수익 중인 종목도 청산 고려."),
    (dtime(11, 30), "【20회차 — 강제 청산】신규 매수 절대 금지. 보유 전종목 즉시 전량 매도."),
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
            run_ctx = _get_run_context(now)

            model = self._model_no_search
            user_message = (
                f"[{today}] {run_ctx}\n"
                "현재 포트폴리오를 점검하고 하이킨아시·VWAP 기준으로 매매를 결정합니다.\n\n"
                "1. get_portfolio()로 현재 포트폴리오를 확인하세요.\n"
                f"2. 수익률 +{TAKE_PROFIT_PCT}% 이상인 종목은 즉시 전량 매도(익절)하세요.\n"
                f"3. 수익률 -{STOP_LOSS_PCT}% 이하인 종목은 즉시 전량 매도(손절)하세요.\n"
                "4. get_top_volume_stocks(n=20)으로 거래량 Top20 스캔 후 하이킨아시·VWAP 기준으로 진입 종목을 선정하세요.\n"
                "5. 분석 결과를 간결하게 요약하세요."
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
                        self.tool_call_log.append({
                            "round": i + 1,
                            "tool": fn.name,
                            "args": args,
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

            self.buy_tickers = list(dict.fromkeys(
                t["args"].get("ticker", "")
                for t in self.tool_call_log
                if t["tool"] == "buy_stock" and t["args"].get("ticker")
            ))

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

    def summarize_session(self, session_log: list[dict]) -> str:
        """5회 루프 결과를 바탕으로 세션 최종 요약 생성 (LLM 1회 호출)."""
        parts = []
        for entry in session_log:
            loop_num = entry["loop"]
            ha_signals = entry.get("ha_signals", [])
            result = entry.get("result") or "신호 없음 — 스킵"

            ha_text = ""
            if ha_signals:
                items = [
                    f"{s['ticker']}({s.get('name', '')}) HA={s['pattern']} VWAP={s['vwap_dev']:+.1f}%"
                    for s in ha_signals
                ]
                ha_text = f"\n  [HA/VWAP] {' | '.join(items)}"

            parts.append(f"【루프 {loop_num}】{ha_text}\n{result[:600]}")

        session_text = "\n\n".join(parts)
        prompt = (
            f"다음은 이번 트레이딩 세션(총 {len(session_log)}회 루프)의 실행 결과입니다:\n\n"
            f"{session_text}\n\n"
            "1. get_portfolio()로 최종 포트폴리오 상태를 확인하세요.\n"
            "2. 이번 세션의 매매 내역(매수/매도 종목, 이유)을 정리하세요.\n"
            "3. 현재 보유 종목의 HA 패턴과 VWAP 상태를 간략히 평가하세요.\n"
            "4. 다음 세션에서 주의할 점을 한 줄로 작성하세요."
        )
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
