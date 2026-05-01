import os
from dotenv import load_dotenv

load_dotenv()

# Google AI Studio
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

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
