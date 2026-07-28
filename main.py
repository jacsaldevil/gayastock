"""
gayastock - BB+RSI 기반 스윙 트레이딩 에이전트
실행: python main.py --once        # 1회 즉시 실행 (내부 루프 포함)
      python main.py --dry-run     # 시뮬레이션 모드
"""
import argparse
import json
import logging
import os
import time
from datetime import timedelta
from data.utils import get_now_kst

logging.Formatter.converter = lambda *args: get_now_kst().timetuple()

_log_dir = os.environ.get("LOG_DIR", "logs")
os.makedirs(_log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(_log_dir, "trading.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

_PROGRESS_BLOB = "session_progress.json"
_PROGRESS_FILE = os.path.join(_log_dir, "session_progress.json")


def _write_progress(data: dict):
    """루프 진행상황을 GCS/로컬에 기록 (대시보드 실시간 표시용)."""
    try:
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass
    bucket = os.environ.get("GCS_DATA_BUCKET", "")
    if bucket:
        try:
            from google.cloud import storage
            storage.Client().bucket(bucket).blob(_PROGRESS_BLOB).upload_from_string(
                json.dumps(data, ensure_ascii=False), content_type="application/json"
            )
        except Exception:
            pass


def is_trading_day() -> bool:
    import holidays
    today = get_now_kst().date()
    if today.weekday() >= 5:
        return False
    kr_holidays = holidays.SouthKorea(years=today.year)
    return today not in kr_holidays


def _check_needs_action(broker, take_profit_pct: float, stop_loss_pct: float,
                        max_positions: int) -> tuple[bool, list[dict]]:
    """Python 사전 체크 — 스윙 트레이딩용. (needs_action, []) 반환."""
    portfolio = broker.get_balance()
    holdings = portfolio.get("holdings", [])

    for h in holdings:
        rate = h.get("profit_loss_rate", 0)
        if rate >= take_profit_pct or rate <= -stop_loss_pct:
            logger.info("TP/SL 조건 해당: %s %.2f%% → Gemini 호출", h.get("ticker"), rate)
            return True, []

    if len(holdings) < max_positions:
        logger.info("빈 슬롯 있음 (%d/%d) → Gemini 호출", len(holdings), max_positions)
        return True, []

    # 스윙 트레이딩: 포지션 풀이어도 BB/RSI 청산 신호 확인을 위해 항상 Gemini 호출
    logger.info("포지션 풀, TP/SL 없음 — BB/RSI 청산 신호 확인을 위해 Gemini 호출")
    return True, []


def _snapshot_market_regime(execute_tool) -> dict:
    """LLM 판단과 별도로 시장 레짐 원본 수치를 저장해 사후 검증 가능하게 한다."""
    try:
        raw = execute_tool("get_market_regime", {})
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"status": "invalid", "raw_type": type(parsed).__name__, "raw": parsed}
    except Exception as exc:
        logger.warning("시장 레짐 진단 스냅샷 실패: %s", exc)
        return {"status": "error", "error": str(exc)}


def _llm_diagnostics(agent, result: str, model_name: str) -> dict:
    """LLM 호출 성공 여부와 실제 함수 호출 경로를 구조화해 기록한다."""
    result = result or ""
    error_prefixes = (
        "Gemini API 초기 호출 실패:",
        "에이전트 루프 중 오류 발생",
    )
    tool_log = list(getattr(agent, "tool_call_log", []) or [])
    return {
        "model": model_name,
        "status": "error" if result.startswith(error_prefixes) else "completed",
        "response_length": len(result),
        "tool_call_count": len(tool_log),
        "tools": [entry.get("tool", "") for entry in tool_log],
    }


def run_trading(watchlist: list[str] | None = None):
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if not dry_run and not is_trading_day() and os.environ.get("FORCE_RUN") != "true":
        logger.info("오늘은 휴장일(공휴일/주말)입니다. 건너뜁니다.")
        return

    from agent.trader import TradingAgent
    from agent.tools import _broker, execute_tool
    from data.trade_log import log_agent_run
    from config import (
        TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS,
        INNER_LOOP_COUNT, INNER_LOOP_SLEEP_SEC, GEMINI_MODEL,
    )
    agent = TradingAgent()
    broker = _broker()

    logger.info("=" * 60)
    logger.info(
        "트레이딩 세션 시작 — 루프 %d회 / 슬립 %d초%s",
        INNER_LOOP_COUNT, INNER_LOOP_SLEEP_SEC,
        "  [DRY-RUN]" if dry_run else "",
    )

    # dry-run + 장외 시간일 때만 1회차 조건 강제
    sim_dt = None
    if dry_run:
        from agent.trader import _SCHEDULE_SLOTS
        from datetime import datetime as _dt, time as _dtime
        import holidays as _hol
        now_kst = get_now_kst()
        market_open = _dtime(9, 0)
        market_close = _dtime(15, 30)
        kr_hol = _hol.SouthKorea(years=now_kst.year)
        in_market = (
            now_kst.weekday() < 5
            and now_kst.date() not in kr_hol
            and market_open <= now_kst.time() <= market_close
        )
        if not in_market:
            # 장외/휴장일 → 가장 최근 거래일 09:00으로 시뮬
            first_slot = _SCHEDULE_SLOTS[0][0]
            check = now_kst.date()
            for _ in range(7):
                if check.weekday() < 5 and check not in kr_hol:
                    break
                check -= timedelta(days=1)
            sim_dt = _dt(check.year, check.month, check.day,
                         first_slot.hour, first_slot.minute,
                         tzinfo=now_kst.tzinfo)
            logger.info("DRY-RUN 장외: sim_datetime=%s (09:00 1회차 강제)", sim_dt.strftime("%Y-%m-%d %H:%M"))
        else:
            logger.info("DRY-RUN 장중: 실제 시각 기준 회차 사용 (%s)", now_kst.strftime("%H:%M"))

    session_id = get_now_kst().strftime("%Y%m%d_%H%M%S")
    progress: dict = {
        "session_id": session_id,
        "source": "scheduled",
        "mode": "dry_run" if dry_run else "live",
        "status": "running",
        "started_at": get_now_kst().isoformat(),
        "total_loops": INNER_LOOP_COUNT,
        "current_loop": 0,
        "loops": [],
    }
    _write_progress(progress)

    session_log: list[dict] = []
    all_buy_tickers: list[str] = []
    sim_portfolio: dict | None = None

    for i in range(INNER_LOOP_COUNT):
        logger.info("--- 루프 %d/%d ---", i + 1, INNER_LOOP_COUNT)
        loop_entry: dict = {
            "loop": i + 1,
            "result": None,
            "market_regime": None,
            "llm": None,
            "tool_log": [],
        }
        loop_p: dict = {
            "loop": i + 1,
            "status": "checking",
            "needs_action": False,
            "market_regime": None,
            "llm": None,
            "tool_log": [],
        }
        progress["current_loop"] = i + 1
        progress["loops"].append(loop_p)
        _write_progress(progress)

        # LLM 호출 전 동일 전략 함수로 레짐 원본 수치를 별도 저장한다.
        market_regime = _snapshot_market_regime(execute_tool)
        loop_entry["market_regime"] = market_regime
        loop_p["market_regime"] = market_regime
        _write_progress(progress)

        if dry_run:
            needs_action = True
        else:
            try:
                needs_action, _ = _check_needs_action(
                    broker, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS,
                )
            except Exception as e:
                logger.warning("사전 체크 오류 (Gemini 폴백): %s", e)
                needs_action = True

        loop_p["needs_action"] = needs_action
        if needs_action:
            loop_p["status"] = "llm_running"
            _write_progress(progress)
            result = agent.run(
                watchlist=watchlist,
                sim_portfolio_in=sim_portfolio,
                cancel_pending=(i == 0),
                skip_log=True,
                sim_datetime=sim_dt,
            )
            if dry_run:
                sim_portfolio = agent.sim_portfolio_out
            loop_entry["result"] = result
            loop_entry["tool_log"] = list(agent.tool_call_log)
            loop_entry["llm"] = _llm_diagnostics(agent, result, GEMINI_MODEL)
            loop_p["result_preview"] = (result or "")[:300]
            loop_p["tool_log"] = list(agent.tool_call_log)[-10:]
            loop_p["llm"] = loop_entry["llm"]
            all_buy_tickers.extend(agent.buy_tickers)
            logger.info("에이전트 결과:\n%s", result)
        else:
            loop_entry["llm"] = {
                "model": GEMINI_MODEL,
                "status": "skipped",
                "response_length": 0,
                "tool_call_count": 0,
                "tools": [],
            }
            loop_p["llm"] = loop_entry["llm"]

        loop_p["status"] = "done"
        _write_progress(progress)
        session_log.append(loop_entry)

        if i < INNER_LOOP_COUNT - 1:
            logger.info("%d초 대기 중...", INNER_LOOP_SLEEP_SEC)
            time.sleep(INNER_LOOP_SLEEP_SEC)

    # 세션 최종 요약 — LLM 1회 호출로 전체 정리
    progress["status"] = "summarizing"
    _write_progress(progress)
    logger.info("=" * 60)
    logger.info("세션 최종 요약 생성 중...")
    final_summary = agent.summarize_session(session_log, sim_portfolio=sim_portfolio if dry_run else None)
    logger.info("최종 요약:\n%s", final_summary)
    if dry_run and sim_portfolio is not None:
        portfolio_snapshot = sim_portfolio
    else:
        try:
            portfolio_snapshot = broker.get_balance()
        except Exception:
            portfolio_snapshot = {}
    log_agent_run(
        watchlist=watchlist,
        summary=final_summary,
        portfolio_snapshot=portfolio_snapshot,
        buy_tickers=list(dict.fromkeys(all_buy_tickers)),
        loops=session_log,
    )
    progress["status"] = "done"
    progress["finished_at"] = get_now_kst().isoformat()
    _write_progress(progress)

    print("\n" + "=" * 60)
    if dry_run:
        print("⚠️  DRY-RUN 모드: 실제 주문이 실행되지 않았습니다.")
    print(final_summary)
    print("=" * 60)
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="gayastock 트레이딩 에이전트")
    parser.add_argument("--once", action="store_true", help="1회 즉시 실행 후 종료")
    parser.add_argument("--dry-run", action="store_true", help="시뮬레이션 모드 (실제 주문 없음)")
    parser.add_argument("--force", action="store_true", help="휴장일 체크 무시 (테스트용)")
    parser.add_argument("--tickers", nargs="+", help="분석할 종목 코드 지정 (예: 005930 000660)")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("DRY-RUN 모드 활성화 — 실제 주문이 실행되지 않습니다.")

    if args.force:
        os.environ["FORCE_RUN"] = "true"

    if args.once:
        run_trading(watchlist=args.tickers)
        return

    logger.info("스케줄러 모드 — Cloud Run Job 방식 권장, 직접 루프는 개발용")
    last_run_id = ""
    while True:
        now_kst = get_now_kst()
        run_id = now_kst.strftime("%Y-%m-%d %H:%M")
        if last_run_id != run_id and now_kst.minute % 11 == 0:
            run_trading()
            last_run_id = run_id
        time.sleep(30)


if __name__ == "__main__":
    main()
