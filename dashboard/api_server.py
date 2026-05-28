"""
로그 조회 API — Streamlit과 함께 실행 (포트 8001)
엔드포인트:
  GET /api/trades      → trades.jsonl 전체 (최근 500건)
  GET /api/agent_runs  → agent_runs.jsonl 전체 (최근 50건)
  GET /api/health      → 헬스체크
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from data.trade_log import get_trades, get_agent_runs

app = FastAPI(title="gayastock log API", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/trades")
def trades(limit: int = 500):
    return JSONResponse(content=get_trades(limit=limit))


@app.get("/api/agent_runs")
def agent_runs(limit: int = 50):
    return JSONResponse(content=get_agent_runs(limit=limit))
