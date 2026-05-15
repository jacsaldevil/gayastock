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
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
INITIAL_CAPITAL = int(os.getenv("INITIAL_CAPITAL") or "0")  # 계좌 이체 원금 (0이면 미설정)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "4.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.5"))

# 내부 루프 설정 (스케줄러 1회 호출당 반복 횟수 / 슬립)
# Cloud Scheduler가 이미 11분마다 실행하므로 기본값 1 — 내부 반복 불필요
INNER_LOOP_COUNT = int(os.getenv("INNER_LOOP_COUNT", "1"))
INNER_LOOP_SLEEP_SEC = int(os.getenv("INNER_LOOP_SLEEP_SEC", "90"))  # 1.5분
