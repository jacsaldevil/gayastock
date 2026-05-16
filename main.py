"""
gayastock - 거래량 모멘텀 + VWAP 국내 주식 트레이딩 에이전트
실행: python main.py --once        # 1회 즉시 실행 (내부 루프 포함)
      python main.py --dry-run     # 시뮬레이션 모드
"""
import argparse
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


def is_trading_day() -> bool:
    import holidays
    today = get_now_kst().date()
    if today.weekday() >= 5:
        return False
    kr_holidays = holidays.Korea(years=today.year)
    return today not in kr_holidays


def _check_needs_action(broker, take_profit_pct: float, stop_loss_pct: float,
                        max_positions: int) -> bool:
    """Python 사전 체크: TP/SL, 빈 슬롯, HA/VWAP 이탈 여부. True면 Gemini 호출 필요."""
    portfolio = broker.get_balance()
    holdings = portfolio.get("holdings", [])

    for h in holdings:
        rate = h.get("profit_loss_rate", 0)
        if rate >= take_profit_pct or rate <= -stop_loss_pct:
            logger.info("TP/SL 조건 해당: %s %.2f%% → Gemini 호출", h.get("ticker"), rate)
            return True

    if len(holdings) < max_positions:
        logger.info("빈 슬롯 있음 (%d/%d) → Gemini 호출", len(holdings), max_positions)
        return True

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
            if not latest.get("bullish", True) or vwap_dev < 0:
                logger.info("HA/VWAP 이탈: %s pattern=%s vwap=%.2f%% → Gemini 호출",
                            ticker, latest.get("pattern"), vwap_dev)
                return True
        except Exception as e:
            logger.warning("HA/VWAP 체크 실패 %s: %s", ticker, e)

    logger.info("포지션 풀, TP/SL 없음, HA/VWAP 정상 → Gemini 스킵")
    return False


def run_trading():
    if not is_trading_day():
        logger.info("오늘은 휴장일(공휴일/주말)입니다. 건너뜁니다.")
        return

    from agent.trader import TradingAgent
    from agent.tools import _broker
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

    for i in range(INNER_LOOP_COUNT):
        is_first = (i == 0)
        logger.info("--- 루프 %d/%d [%s] ---", i + 1, INNER_LOOP_COUNT,
                    "전체 스캔" if is_first else "빠른 점검(매수·매도)")

        # dry-run은 sim 포트폴리오를 agent가 관리하므로 항상 호출
        if dry_run:
            needs_action = True
        else:
            try:
                needs_action = _check_needs_action(
                    broker, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS,
                )
            except Exception as e:
                logger.warning("사전 체크 오류 (Gemini 폴백): %s", e)
                needs_action = True

        if needs_action:
            result = agent.run(
                cancel_pending=is_first,
                skip_log=False,
                sim_datetime=sim_dt,
                light_mode=not is_first,
            )
            logger.info("에이전트 결과:\n%s", result)
            if is_first:
                print("\n" + "=" * 60)
                if dry_run:
                    print("⚠️  DRY-RUN 모드: 실제 주문이 실행되지 않았습니다.")
                print(result)
                print("=" * 60)

        if i < INNER_LOOP_COUNT - 1:
            # 첫 루프 후는 INNER_LOOP_SLEEP_SEC, 이후 루프 사이는 30초
            sleep_sec = INNER_LOOP_SLEEP_SEC if is_first else 30
            logger.info("%d초 대기 중...", sleep_sec)
            time.sleep(sleep_sec)

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
