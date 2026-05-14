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


def _check_needs_action(broker, take_profit_pct: float, stop_loss_pct: float, max_positions: int) -> bool:
    """Python 사전 체크: TP/SL 조건 또는 빈 슬롯 여부. True면 Gemini 호출 필요."""
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

    logger.info("포지션 풀, TP/SL 없음 → Gemini 스킵")
    return False


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

    summaries: list[str] = []
    all_buy_tickers: list[str] = []

    for i in range(INNER_LOOP_COUNT):
        logger.info("--- 루프 %d/%d ---", i + 1, INNER_LOOP_COUNT)

        # dry-run은 sim 포트폴리오를 agent가 관리하므로 항상 호출
        if dry_run:
            needs_action = True
        else:
            try:
                needs_action = _check_needs_action(broker, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS)
            except Exception as e:
                logger.warning("사전 체크 오류 (Gemini 폴백): %s", e)
                needs_action = True

        if needs_action:
            result = agent.run(cancel_pending=(i == 0), skip_log=True)
            summaries.append(result)
            for t in agent.buy_tickers:
                if t not in all_buy_tickers:
                    all_buy_tickers.append(t)
            logger.info("에이전트 결과:\n%s", result)
            if i == 0:
                print("\n" + "=" * 60)
                if dry_run:
                    print("⚠️  DRY-RUN 모드: 실제 주문이 실행되지 않았습니다.")
                print(result)
                print("=" * 60)

        if i < INNER_LOOP_COUNT - 1:
            logger.info("%d초 대기 중...", INNER_LOOP_SLEEP_SEC)
            time.sleep(INNER_LOOP_SLEEP_SEC)

    # 세션 전체(루프 4회)를 하나의 로그 엔트리로 기록
    if summaries:
        combined = "\n\n---\n\n".join(summaries)
        try:
            portfolio_snapshot = broker.get_balance()
        except Exception:
            portfolio_snapshot = agent.sim_portfolio_out or {}
        log_agent_run(None, combined, portfolio_snapshot, buy_tickers=all_buy_tickers)

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
