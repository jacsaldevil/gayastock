import os
from dotenv import load_dotenv

load_dotenv()

# Vertex AI
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
GCP_REGION = os.getenv("GCP_REGION", "asia-northeast3")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# KIS API
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
KIS_MOCK = os.getenv("KIS_MOCK", "true").lower() == "true"

KIS_BASE_URL = (
    "https://openapivts.koreainvestment.com:29443"  # 모의투자
    if KIS_MOCK else
    "https://openapi.koreainvestment.com:9443"       # 실투자
)

# 트레이딩 설정
MAX_BUY_AMOUNT = int(os.getenv("MAX_BUY_AMOUNT", "500000"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))
INITIAL_CAPITAL = int(os.getenv("INITIAL_CAPITAL") or "0")  # 계좌 이체 원금 (0이면 미설정)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "8.0"))   # 1차 부분익절 기준
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "7.0"))       # 절대 손절 상한

# 변동성 기반 포지션 사이징
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "1.5"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "40.0"))
MIN_STOCK_PRICE = int(os.getenv("MIN_STOCK_PRICE", "3000"))

# 중기 트레이딩 기술적 지표 설정
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))          # 볼린저밴드 기간 (일봉)
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))        # RSI 기간 (일봉)
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "20"))  # 최대 보유 영업일 수

# 시장 레짐 필터 (KODEX 200을 코스피 대형주 프록시로 사용)
MARKET_PROXY_TICKER = os.getenv("MARKET_PROXY_TICKER", "069500")
MARKET_CRASH_PCT = float(os.getenv("MARKET_CRASH_PCT", "-5.0"))

# 기존 도구 호환용 RSI 상한. v12 실제 진입 상한은 setup별 규칙에서 별도 검증.
RSI_MAX_ENTRY = float(os.getenv("RSI_MAX_ENTRY", "72.0"))
MAX_DAILY_BUY_PER_TICKER = int(os.getenv("MAX_DAILY_BUY_PER_TICKER", "1"))

# 내부 루프 설정 (스케줄러 1회 호출당 반복 횟수 / 슬립)
# Cloud Scheduler가 30분마다 실행하므로 기본값 1
INNER_LOOP_COUNT = int(os.getenv("INNER_LOOP_COUNT", "1"))
INNER_LOOP_SLEEP_SEC = int(os.getenv("INNER_LOOP_SLEEP_SEC", "90"))  # 1.5분
