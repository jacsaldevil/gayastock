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
from datetime import date

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

# KOSPI 우량주 20종목 — 8개 섹터 분산
DEFAULT_WATCHLIST = [
    # 반도체/전자
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "066570",  # LG전자
    # 인터넷/플랫폼
    "035420",  # NAVER
    "035720",  # 카카오
    # 자동차
    "005380",  # 현대차
    "000270",  # 기아
    "012330",  # 현대모비스
    # 2차전지/화학
    "373220",  # LG에너지솔루션
    "006400",  # 삼성SDI
    "051910",  # LG화학
    # 철강/소재
    "005490",  # POSCO홀딩스
    "010130",  # 고려아연
    # 금융
    "105560",  # KB금융
    "055550",  # 신한지주
    "086790",  # 하나금융지주
    # 바이오
    "207940",  # 삼성바이오로직스
    "068270",  # 셀트리온
    # 통신/지주
    "017670",  # SK텔레콤
    "028260",  # 삼성물산
]


def is_trading_day() -> bool:
    import holidays
    today = date.today()
    if today.weekday() >= 5:
        return False
    kr_holidays = holidays.Korea(years=today.year)
    return today not in kr_holidays


def run_trading(watchlist: list[str]):
    if not is_trading_day():
        logger.info("오늘은 휴장일(공휴일/주말)입니다. 건너뜁니다.")
        return
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

    # 09:10 장 시작 / 12:00 점심 / 14:30 마감 1시간 전 — 하루 3회
    import schedule
    schedule.every().day.at("09:10").do(run_trading, watchlist=watchlist)
    schedule.every().day.at("12:00").do(run_trading, watchlist=watchlist)
    schedule.every().day.at("14:30").do(run_trading, watchlist=watchlist)

    logger.info(f"스케줄러 시작 (09:10, 12:00, 14:30 실행) | 관심종목: {len(watchlist)}종목")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
