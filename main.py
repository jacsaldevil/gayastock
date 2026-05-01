"""
gayastock - 재무제표 기반 국내 주식 트레이딩 에이전트
실행: python main.py
      python main.py --once        # 1회 즉시 실행
      python main.py --tickers 005930 000660  # 특정 종목만
"""
import argparse
import logging
import os
import time

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

# 기본 관심종목 (KOSPI 대형주 예시)
DEFAULT_WATCHLIST = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "035420",  # NAVER
    "051910",  # LG화학
    "006400",  # 삼성SDI
]


def run_trading(watchlist: list[str]):
    from agent.trader import TradingAgent
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    agent = TradingAgent()
    logger.info("=" * 60)
    logger.info(f"트레이딩 세션 시작{'  [DRY-RUN 시뮬레이션]' if dry_run else ''}")
    result = agent.run(watchlist)
    logger.info("에이전트 판단 결과:\n" + result)
    logger.info("=" * 60)
    print("\n" + "=" * 60)
    if dry_run:
        print("⚠️  DRY-RUN 모드: 실제 주문이 실행되지 않았습니다.")
    print(result)
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="gayastock 트레이딩 에이전트")
    parser.add_argument("--once", action="store_true", help="1회 즉시 실행 후 종료")
    parser.add_argument("--dry-run", action="store_true", help="시뮬레이션 모드 (실제 주문 없음)")
    parser.add_argument("--tickers", nargs="+", help="분석할 종목코드 목록")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("DRY-RUN 모드 활성화 — 실제 주문이 실행되지 않습니다.")

    watchlist = args.tickers if args.tickers else DEFAULT_WATCHLIST

    if args.once:
        run_trading(watchlist)
        return

    # 장 시작 후 (09:10) 와 오후 (14:00) 하루 2회 실행
    import schedule
    schedule.every().day.at("09:10").do(run_trading, watchlist=watchlist)
    schedule.every().day.at("14:00").do(run_trading, watchlist=watchlist)

    logger.info(f"스케줄러 시작 (09:10, 14:00 실행) | 관심종목: {watchlist}")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
