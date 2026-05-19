"""
gayastock - 거래량 모멘텀 + VWAP 국내 주식 트레이딩 에이전트
실행: python main.py --once        # 1회 즉시 실행 (내부 루프 포함)
      python main.py --dry-run     # 시뮬레이션 모드
"""
import argparse
import json
import logging
import os
import time
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
    kr_holidays = holidays.Korea(years=today.year)
    return today not in kr_holidays


def _check_needs_action(broker, take_profit_pct: float, stop_loss_pct: float,
                        max_positions: int) -> tuple[bool, list[dict]]:
    """Python 사전 체크. (needs_action, ha_signals) 반환."""
    ha_signals = []
    portfolio = broker.get_balance()
    holdings = portfolio.get("holdings", [])

    for h in holdings:
        rate = h.get("profit_loss_rate", 0)
        if rate >= take_profit_pct or rate <= -stop_loss_pct:
            logger.info("TP/SL 조건 해당: %s %.2f%% → Gemini 호출", h.get("ticker"), rate)
            return True, ha_signals

    if len(holdings) < max_positions:
        logger.info("빈 슬롯 있음 (%d/%d) → Gemini 호출", len(holdings), max_positions)
        return True, ha_signals

    # 포지션 풀 — 보유 종목 HA/VWAP 이탈 체크 (매도 신호 감지)
    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        try:
            result = broker.get_minute_candles(ticker)
            candles = result.get("candles", [])
            vwap_dev = result.get("vwap_deviation_pct", 0)
            if not candles:
                continue
            latest = candles[-1]
            signal = {
                "ticker": ticker,
                "name": result.get("name", ""),
                "pattern": latest.get("pattern", ""),
                "vwap_dev": round(float(vwap_dev), 2),
                "bullish": latest.get("bullish", True),
            }
            ha_signals.append(signal)
            if vwap_dev < 0:
                logger.info("VWAP 이탈: %s vwap=%.2f%% → Gemini 호출", ticker, vwap_dev)
                return True, ha_signals
        except Exception as e:
            logger.warning("HA/VWAP 체크 실패 %s: %s", ticker, e)

    logger.info("포지션 풀, TP/SL 없음, HA/VWAP 정상 → Gemini 스킵")
    return False, ha_signals


def run_trading():
    if not is_trading_day():
        logger.info("오늘은 휴장일(공휴일/주말)입니다. 건너뜁니다.")
        return

    from agent.trader import TradingAgent
    from agent.tools import _broker
    from data.trade_log import log_agent_run
    from config import (
        TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS,
        INNER_LOOP_COUNT, INNER_LOOP_SLEEP_SEC,
    )

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
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
        now_kst = get_now_kst()
        market_open = _dtime(9, 0)
        market_close = _dtime(15, 30)
        in_market = market_open <= now_kst.time() <= market_close
        if not in_market:
            first_slot = _SCHEDULE_SLOTS[0][0]
            sim_dt = _dt(now_kst.year, now_kst.month, now_kst.day,
                         first_slot.hour, first_slot.minute,
                         tzinfo=now_kst.tzinfo)
            logger.info("DRY-RUN 장외: sim_datetime=%s (1회차 강제)", sim_dt.strftime("%H:%M"))
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

    for i in range(INNER_LOOP_COUNT):
        logger.info("--- 루프 %d/%d ---", i + 1, INNER_LOOP_COUNT)
        loop_entry: dict = {"loop": i + 1, "ha_signals": [], "result": None}
        loop_p: dict = {"loop": i + 1, "status": "checking", "ha_signals": [], "needs_action": False}
        progress["current_loop"] = i + 1
        progress["loops"].append(loop_p)
        _write_progress(progress)

        if dry_run:
            needs_action = True
        else:
            try:
                needs_action, ha_signals = _check_needs_action(
                    broker, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS,
                )
                loop_entry["ha_signals"] = ha_signals
                loop_p["ha_signals"] = ha_signals
            except Exception as e:
                logger.warning("사전 체크 오류 (Gemini 폴백): %s", e)
                needs_action = True

        loop_p["needs_action"] = needs_action
        if needs_action:
            loop_p["status"] = "llm_running"
            _write_progress(progress)
            result = agent.run(
                cancel_pending=(i == 0),
                skip_log=True,
                sim_datetime=sim_dt,
            )
            loop_entry["result"] = result
            loop_p["result_preview"] = (result or "")[:300]
            all_buy_tickers.extend(agent.buy_tickers)
            logger.info("에이전트 결과:\n%s", result)

        loop_p["status"] = "done"
        _write_progress(progress)
        session_log.append(loop_entry)

        if i < INNER_LOOP_COUNT - 1:
            logger.info("30초 대기 중...")
            time.sleep(30)

    # 세션 최종 요약 — LLM 1회 호출로 전체 정리
    progress["status"] = "summarizing"
    _write_progress(progress)
    logger.info("=" * 60)
    logger.info("세션 최종 요약 생성 중...")
    final_summary = agent.summarize_session(session_log)
    logger.info("최종 요약:\n%s", final_summary)
    try:
        portfolio_snapshot = broker.get_balance()
    except Exception:
        portfolio_snapshot = {}
    log_agent_run(
        watchlist=None,
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
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("DRY-RUN 모드 활성화 — 실제 주문이 실행되지 않습니다.")

    if args.once:
        run_trading()
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
