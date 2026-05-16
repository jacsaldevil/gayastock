"""매매 이력 및 에이전트 판단 로그를 GCS(운영) / 로칼 JSONL(개발)로 저장/조회"""
import json
import logging
import os
from datetime import datetime
from data.utils import get_now_kst

logger = logging.getLogger(__name__)

_LOG_DIR = os.getenv("LOG_DIR", "logs")
TRADE_LOG_FILE = os.path.join(_LOG_DIR, "trades.jsonl")
AGENT_LOG_FILE = os.path.join(_LOG_DIR, "agent_runs.jsonl")

# GCS_DATA_BUCKET 환경변수가 있으면 GCS 사용, 없으면 로칼 파일
_GCS_BUCKET = os.environ.get("GCS_DATA_BUCKET", "")
_TRADE_BLOB = "logs/trades.jsonl"
_AGENT_BLOB = "logs/agent_runs.jsonl"


# ── GCS 헬퍼 ────────────────────────────

def _gcs_read_lines(blob_name: str) -> list[str]:
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(_GCS_BUCKET).blob(blob_name)
        if not blob.exists():
            return []
        return blob.download_as_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.debug("GCS 읽기 실패 (%s): %s", blob_name, e)
        return []


def _gcs_append_line(blob_name: str, line: str, max_records: int | None = None) -> bool:
    try:
        from google.cloud import storage
        bucket = storage.Client().bucket(_GCS_BUCKET)
        blob = bucket.blob(blob_name)
        existing = blob.download_as_text(encoding="utf-8") if blob.exists() else ""
        lines = [l for l in existing.splitlines() if l.strip()]
        lines.append(line)
        if max_records:
            lines = lines[-max_records:]
        blob.upload_from_string("\n".join(lines) + "\n",
                                content_type="text/plain; charset=utf-8")
        return True
    except Exception as e:
        logger.warning("GCS 쓰기 실패 (%s): %s", blob_name, e)
        return False


# ── 로칼 파일 헬퍼 ────────────────────

def _local_append(filepath: str, line: str, max_records: int | None = None):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if max_records:
        lines = _local_read_lines(filepath)
        lines.append(line)
        lines = lines[-max_records:]
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    else:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _local_read_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        return []
    with open(filepath, encoding="utf-8") as f:
        return f.read().splitlines()


# ── 공통 ─────────────────────────

def _append(blob_name: str, filepath: str, record: dict, max_records: int | None = None):
    line = json.dumps(record, ensure_ascii=False)
    if _GCS_BUCKET:
        if _gcs_append_line(blob_name, line, max_records):
            return
        logger.warning("GCS 쓰기 실패 — 로컬 파일 fallback: %s", filepath)
    _local_append(filepath, line, max_records)


def _read_all(blob_name: str, filepath: str) -> list[dict]:
    lines = _gcs_read_lines(blob_name) if _GCS_BUCKET else _local_read_lines(filepath)
    records = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


# ── 공개 API ───────────────────────

_MAX_TRADE_RECORDS = 1000    # 매매 로그 최대 보존 건수
_MAX_AGENT_RECORDS = 500     # 에이전트 실행 로그 최대 보존 건수 (20회/일 × 25일)


def log_trade(action: str, ticker: str, quantity: int, price: int, reason: str, success: bool, name: str = ""):
    _append(_TRADE_BLOB, TRADE_LOG_FILE, {
        "ts": get_now_kst().isoformat(),
        "action": action,
        "ticker": ticker,
        "name": name,
        "quantity": quantity,
        "price": price,
        "amount": price * quantity,
        "reason": reason,
        "success": success,
    }, max_records=_MAX_TRADE_RECORDS)


def log_agent_run(watchlist: list[str], summary: str, portfolio_snapshot: dict,
                  buy_tickers: list = None, loops: list = None):
    _append(_AGENT_BLOB, AGENT_LOG_FILE, {
        "ts": get_now_kst().isoformat(),
        "watchlist": watchlist,
        "summary": summary,
        "portfolio": portfolio_snapshot,
        "buy_tickers": buy_tickers or [],
        "loops": loops or [],
    }, max_records=_MAX_AGENT_RECORDS)


def get_trades(limit: int = 200) -> list[dict]:
    return _read_all(_TRADE_BLOB, TRADE_LOG_FILE)[-limit:]


def get_agent_runs(limit: int = 50) -> list[dict]:
    return _read_all(_AGENT_BLOB, AGENT_LOG_FILE)[-limit:]
