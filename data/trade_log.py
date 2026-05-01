"""매매 이력 및 에이전트 판단 로그를 JSON Lines 파일로 저장/조회"""
import json
import os
from datetime import datetime

TRADE_LOG_FILE = "logs/trades.jsonl"
AGENT_LOG_FILE = "logs/agent_runs.jsonl"


def _append(filepath: str, record: dict):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_all(filepath: str) -> list[dict]:
    if not os.path.exists(filepath):
        return []
    records = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def log_trade(action: str, ticker: str, quantity: int, price: int, reason: str, success: bool):
    """매수/매도 실행 이력 기록"""
    _append(TRADE_LOG_FILE, {
        "ts": datetime.now().isoformat(),
        "action": action,       # "BUY" | "SELL"
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "amount": price * quantity,
        "reason": reason,
        "success": success,
    })


def log_agent_run(watchlist: list[str], summary: str, portfolio_snapshot: dict):
    """에이전트 1회 실행 결과 요약 기록"""
    _append(AGENT_LOG_FILE, {
        "ts": datetime.now().isoformat(),
        "watchlist": watchlist,
        "summary": summary,
        "portfolio": portfolio_snapshot,
    })


def get_trades(limit: int = 200) -> list[dict]:
    return _read_all(TRADE_LOG_FILE)[-limit:]


def get_agent_runs(limit: int = 50) -> list[dict]:
    return _read_all(AGENT_LOG_FILE)[-limit:]
