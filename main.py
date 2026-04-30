"""
gayastock - 재무제표 기반 국내 주식 트레이딩 에이전트
실행: python main.py
      python main.py --once        # 1회 즉시 실행
      python main.py --tickers 005930 000660  # 특정 종목만
"""
import argparse
import logging
import schedule
import time
from agent.trader import TradingAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/trading.log", encoding="utf-8"),
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
    agent = TradingAgent()
    logger.info("=" * 60)
    logger.info("트레이딩 세션 시작")
    result = agent.run(watchlist)
    logger.info("에이전트 판단 결과:\n" + result)
    logger.info("=" * 60)
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="gayastock 트레이딩 에이전트")
    parser.add_argument("--once", action="store_true", help="1회 즉시 실행 후 종료")
    parser.add_argument("--tickers", nargs="+", help="분석할 종목코드 목록")
    args = parser.parse_args()

    watchlist = args.tickers if args.tickers else DEFAULT_WATCHLIST

    if args.once:
        run_trading(watchlist)
        return

    # 장 시작 후 (09:10) 와 오후 (14:00) 하루 2회 실행
    schedule.every().day.at("09:10").do(run_trading, watchlist=watchlist)
    schedule.every().day.at("14:00").do(run_trading, watchlist=watchlist)

    logger.info(f"스케줄러 시작 (09:10, 14:00 실행) | 관심종목: {watchlist}")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
