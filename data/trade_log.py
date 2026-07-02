"""매매 이력 및 에이전트 판단 로그를 GCS(운영) / 로칼 JSONL(개발)로 저장/조회"""
import json
import logging
import os
import threading
import time
from data.utils import get_now_kst

logger = logging.getLogger(__name__)

_LOG_DIR = os.getenv("LOG_DIR", "logs")
TRADE_LOG_FILE = os.path.join(_LOG_DIR, "trades.jsonl")
AGENT_LOG_FILE = os.path.join(_LOG_DIR, "agent_runs.jsonl")

# GCS_DATA_BUCKET 환경변수가 있으면 GCS 사용, 없으면 로칼 파일
_GCS_BUCKET = os.environ.get("GCS_DATA_BUCKET", "")
_TRADE_BLOB = "logs/trades.jsonl"
_AGENT_BLOB = "logs/agent_runs.jsonl"


# ── 날짜 기반 트리밍 ─────────────────────

def _trim_to_latest_day(existing: list[str], new_line: str) -> list[str]:
    """새 레코드가 기존 최신 날짜보다 미래면 이전 로그 전체 삭제 (장 오픈일 단위 보존)."""
    if not existing:
        return [new_line]
    try:
        new_date = json.loads(new_line).get("ts", "")[:10]
    except Exception:
        return existing + [new_line]
    if not new_date:
        return existing + [new_line]
    # 기존 레코드 중 최신 날짜를 역순 탐색으로 빠르게 찾기
    latest_existing = None
    for line in reversed(existing):
        if not line.strip():
            continue
        try:
            ts = json.loads(line).get("ts", "")[:10]
            if ts:
                latest_existing = ts
                break
        except Exception:
            break
    if latest_existing and new_date > latest_existing:
        logger.info("새 거래일 감지 (%s → %s) — 이전 로그 삭제", latest_existing, new_date)
        return [new_line]
    return existing + [new_line]


# ── GCS 헬퍼 ────────────────────────────

_gcs_lock = threading.Lock()  # 프로세스 내 동시 쓰기 직렬화


def _gcs_read_lines(blob_name: str) -> list[str]:
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(_GCS_BUCKET).blob(blob_name)
        return blob.download_as_text(encoding="utf-8").splitlines()
    except Exception as e:
        if "404" in str(e) or "NotFound" in type(e).__name__:
            return []
        logger.warning("GCS 읽기 실패 (%s): %s", blob_name, e)
        return []


def _gcs_append_line(blob_name: str, line: str) -> bool:
    """generation match + 재시도로 동시 쓰기 충돌 방지."""
    try:
        from google.cloud import storage
        from google.api_core.exceptions import PreconditionFailed

        with _gcs_lock:
            for attempt in range(5):
                try:
                    bucket = storage.Client().bucket(_GCS_BUCKET)
                    blob = bucket.blob(blob_name)
                    try:
                        blob.reload()
                        generation = blob.generation
                        existing = blob.download_as_text(encoding="utf-8")
                    except Exception as _e:
                        if "404" in str(_e) or "NotFound" in type(_e).__name__:
                            generation = 0
                            existing = ""
                        else:
                            raise

                    existing_lines = [line_text for line_text in existing.splitlines() if line_text.strip()]
                    lines = _trim_to_latest_day(existing_lines, line)

                    blob.upload_from_string(
                        "\n".join(lines) + "\n",
                        content_type="text/plain; charset=utf-8",
                        if_generation_match=generation,
                    )
                    try:
                        blob.make_public()
                    except Exception:
                        pass  # Uniform Bucket-Level Access 사용 시 IAM으로 처리
                    return True

                except PreconditionFailed:
                    # 다른 프로세스가 먼저 썼으면 재시도
                    if attempt < 4:
                        time.sleep(0.2 * (2 ** attempt))  # 0.2 → 0.4 → 0.8 → 1.6초

            logger.warning("GCS 동시 쓰기 충돌 — 재시도 5회 초과: %s", blob_name)
            return False

    except Exception as e:
        logger.warning("GCS 쓰기 실패 (%s): %s", blob_name, e)
        return False


# ── 로칼 파일 헬퍼 ────────────────────

def _local_append(filepath: str, line: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    existing = [line_text for line_text in _local_read_lines(filepath) if line_text.strip()]
    lines = _trim_to_latest_day(existing, line)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _local_read_lines(filepath: str) -> list[str]:
    if not os.path.exists(filepath):
        return []
    with open(filepath, encoding="utf-8") as f:
        return f.read().splitlines()


# ── 공통 ─────────────────────────

def _append(blob_name: str, filepath: str, record: dict):
    line = json.dumps(record, ensure_ascii=False)
    if _GCS_BUCKET:
        if _gcs_append_line(blob_name, line):
            return
        logger.warning("GCS 쓰기 실패 — 로컬 파일 fallback: %s", filepath)
    _local_append(filepath, line)


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


def log_trade(action: str, ticker: str, quantity: int, price: int, reason: str, success: bool,
              name: str = "", profit: int = 0,
              vwap_dev: float | None = None, ha_pattern: str = "",
              bb_signal: str = "", rsi_value: float | None = None):
    record: dict = {
        "ts": get_now_kst().isoformat(),
        "action": action,
        "ticker": ticker,
        "name": name,
        "quantity": quantity,
        "price": price,
        "amount": price * quantity,
        "reason": reason,
        "success": success,
    }
    if vwap_dev is not None:
        record["vwap_dev"] = round(float(vwap_dev), 2)
    if ha_pattern:
        record["ha_pattern"] = ha_pattern
    if bb_signal:
        record["bb_signal"] = bb_signal
    if rsi_value is not None:
        record["rsi_value"] = round(float(rsi_value), 2)
    if action == "SELL":
        record["profit"] = profit
    _append(_TRADE_BLOB, TRADE_LOG_FILE, record)


def log_cancel(ticker: str, quantity: int, price: int, order_no: str, name: str = ""):
    """미체결 주문 취소 기록 — BUY 접수 후 미체결 취소 시 trade_log 정합성 유지용."""
    _append(_TRADE_BLOB, TRADE_LOG_FILE, {
        "ts": get_now_kst().isoformat(),
        "action": "CANCEL",
        "ticker": ticker,
        "name": name,
        "quantity": quantity,
        "price": price,
        "amount": price * quantity,
        "order_no": order_no,
        "reason": "미체결 주문 취소",
        "success": True,
    })


def log_agent_run(watchlist: list[str], summary: str, portfolio_snapshot: dict,
                  buy_tickers: list = None, loops: list = None):
    _append(_AGENT_BLOB, AGENT_LOG_FILE, {
        "ts": get_now_kst().isoformat(),
        "watchlist": watchlist,
        "summary": summary,
        "portfolio": portfolio_snapshot,
        "buy_tickers": buy_tickers or [],
        "loops": loops or [],
    })


def get_trades(limit: int = 200) -> list[dict]:
    return _read_all(_TRADE_BLOB, TRADE_LOG_FILE)[-limit:]


def get_agent_runs(limit: int = 50) -> list[dict]:
    return _read_all(_AGENT_BLOB, AGENT_LOG_FILE)[-limit:]
