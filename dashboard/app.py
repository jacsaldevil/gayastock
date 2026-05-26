"""gayastock 대시보드 — streamlit run dashboard/app.py"""
import sys
import os
import json
import logging
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

from datetime import datetime, timedelta
from data.utils import get_now_kst, KST
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from data.financial import _get_broker as _get_kis_broker
from data.trade_log import get_trades, get_agent_runs
from config import INITIAL_CAPITAL as _INITIAL_CAPITAL_ENV

_GCS_DATA_BUCKET = os.environ.get("GCS_DATA_BUCKET", "")
_SETTINGS_BLOB = "settings.json"


_settings_load_error: str = ""
_settings_load_info: str = ""


def _load_settings() -> dict:
    """GCS 공개 URL → 인증 읽기 → 로컬 순으로 설정 읽기"""
    global _settings_load_error, _settings_load_info
    _settings_load_error = ""
    _settings_load_info = ""
    if _GCS_DATA_BUCKET:
        import urllib.request, urllib.error
        bucket = _GCS_DATA_BUCKET.strip()
        url = f"https://storage.googleapis.com/{bucket}/{_SETTINGS_BLOB}"
        # 1단계: 공개 URL로 읽기 (인증 없이)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                _settings_load_info = f"공개 URL 읽기 성공: {data}"
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _settings_load_info = f"settings.json 없음 (공개 URL 404) — 아직 저장 안 됨"
            else:
                _settings_load_info = f"공개 URL {e.code}: {e.reason} — 인증 읽기 시도"
        except Exception as e:
            _settings_load_info = f"공개 URL 실패: {type(e).__name__} — 인증 읽기 시도"

        # 2단계: 인증된 GCS 클라이언트로 읽기
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(bucket).blob(_SETTINGS_BLOB)
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            _settings_load_info += f" | 인증 읽기 성공: {data}"
            return data
        except Exception as e2:
            err = str(e2)
            if "404" in err or "NotFound" in type(e2).__name__:
                _settings_load_info += " | 인증 읽기: 404 없음"
                return {}
            _settings_load_error = f"[{type(e2).__name__}] {err[:120]}"
            logger.warning("settings GCS 인증 읽기 실패: %s", e2)
            return {}
    try:
        with open(".dashboard_settings.json", encoding="utf-8") as f:
            data = json.load(f)
            _settings_load_info = f"로컬 파일 읽기 성공: {data}"
            return data
    except Exception:
        return {}


def _save_settings(data: dict) -> str:
    """저장 위치 반환: 'gcs' | 'local' | 'error'"""
    existing = _load_settings()
    existing.update(data)
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET.strip()).blob(_SETTINGS_BLOB)
            blob.upload_from_string(json.dumps(existing), content_type="application/json")
            try:
                blob.make_public()  # 공개 URL 읽기 가능하도록
            except Exception:
                pass  # Uniform Bucket-Level Access 환경에서는 IAM으로 처리됨
            return "gcs"
        except Exception as e:
            logger.warning("settings GCS 저장 실패: %s", e)
    try:
        with open(".dashboard_settings.json", "w", encoding="utf-8") as f:
            json.dump(existing, f)
        return "local"
    except Exception:
        return "error"


def _get_initial_capital() -> int:
    """세션 상태 → GCS 설정 → 환경변수 순으로 투자금액 반환"""
    # 저장 직후 세션 내에서 즉시 반영되도록 session_state 우선 사용
    if "initial_capital" in st.session_state:
        return int(st.session_state["initial_capital"])
    settings = _load_settings()
    val = settings.get("initial_capital", 0)
    result = val if val > 0 else _INITIAL_CAPITAL_ENV
    st.session_state["initial_capital"] = result
    return result


def _get_sim_datetime(live: bool):
    """시뮬(not live): 최근 거래일의 1회차(09:20) 시간으로 강제. 실전: None(실제 시각)."""
    if live:
        return None
    from agent.trader import _SCHEDULE_SLOTS
    import holidays as _hol
    first_slot = _SCHEDULE_SLOTS[0][0]
    now_kst = get_now_kst()
    check = now_kst.date()
    kr_hol = _hol.SouthKorea(years=check.year)
    for _ in range(7):
        if check.weekday() < 5 and check not in kr_hol:
            break
        check -= timedelta(days=1)
    return datetime(check.year, check.month, check.day,
                    first_slot.hour, first_slot.minute,
                    tzinfo=now_kst.tzinfo)

st.set_page_config(
    page_title="gayastock 대시보드",
    page_icon="📈",
    layout="wide",
)

# ── 전역 모바일 테이블 CSS ────────────────
st.markdown("""
<style>
[data-testid="stMarkdownContainer"] table {
    font-size: 11.5px;
    border-collapse: collapse;
    display: block;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    max-width: 100%;
}
[data-testid="stMarkdownContainer"] th,
[data-testid="stMarkdownContainer"] td {
    padding: 2px 6px !important;
    white-space: nowrap;
}
[data-testid="stMarkdownContainer"] th {
    background-color: rgba(255,255,255,0.07);
}
</style>
""", unsafe_allow_html=True)

# ── 사이드바 ──────────────────────
st.sidebar.title("📈 gayastock")
st.sidebar.caption("AI 주식 트레이딩 에이전트")
page = st.sidebar.radio("메뉴", ["포트폴리오", "매매 이력", "에이전트 로그"])
refresh = st.sidebar.button("🔄 새로고침")

st.sidebar.divider()
st.sidebar.markdown("**💰 투자금액 설정**")

_CAPITAL_KEY = "capital_input_widget"
_cur_capital = _get_initial_capital()
if _settings_load_error:
    st.sidebar.error(f"⚠️ GCS 읽기 오류: {_settings_load_error}")

with st.sidebar.expander("🔍 설정 진단", expanded=bool(_settings_load_error)):
    st.caption(f"버킷: `{_GCS_DATA_BUCKET or '미설정'}`")
    st.caption(f"파일: `{_SETTINGS_BLOB}`")
    st.caption(f"환경변수 INITIAL_CAPITAL: `{_INITIAL_CAPITAL_ENV:,}`")
    st.caption(f"현재 투자금액: `{_cur_capital:,}`")
    if _settings_load_info:
        st.caption(f"읽기 결과: {_settings_load_info}")
    if _settings_load_error:
        st.caption(f"오류: {_settings_load_error}")

_new_capital = st.sidebar.number_input(
    "투자 원금 (원)", min_value=0, step=10000,
    value=_cur_capital, format="%d",
    help="0이면 현재 예수금 기준으로 표시됩니다.",
    label_visibility="collapsed",
    key=_CAPITAL_KEY,
)
if st.sidebar.button("저장", use_container_width=True):
    _save_val = int(_new_capital)
    _where = _save_settings({"initial_capital": _save_val})
    st.cache_data.clear()
    if _where in ("gcs", "local"):
        # 세션 상태 즉시 업데이트 — GCS 읽기 성공 여부와 무관하게 이번 세션에 반영
        st.session_state["initial_capital"] = _save_val
        st.session_state.pop(_CAPITAL_KEY, None)  # 위젯도 새 값으로 초기화
    if _where == "gcs":
        st.sidebar.success("✅ GCS 저장 완료 (영구 보존)")
    elif _where == "local":
        st.sidebar.warning("⚠️ 로컬 저장 (재배포 시 초기화)")
    else:
        st.sidebar.error("❌ 저장 실패")
    st.rerun()
if not _GCS_DATA_BUCKET:
    st.sidebar.caption("⚠️ GCS 미설정 — 재배포 시 초기화됨")
else:
    st.sidebar.divider()
    st.sidebar.markdown("**🔗 로그 직접 접근 URL**")
    _base = f"https://storage.googleapis.com/{_GCS_DATA_BUCKET}"
    st.sidebar.code(f"{_base}/logs/trades.jsonl", language=None)
    st.sidebar.code(f"{_base}/logs/agent_runs.jsonl", language=None)

@st.cache_data(ttl=30)
def load_balance():
    try:
        return _get_kis_broker().get_balance()
    except Exception as e:
        return {"error": str(e)}


@st.cache_data(ttl=120)
def _fetch_candles(ticker: str) -> dict:
    try:
        return _get_kis_broker().get_minute_candles(ticker)
    except Exception:
        return {}


def _fmt_time(t: str) -> str:
    """HHMMSS → HH:MM"""
    if len(t) >= 4:
        return f"{t[:2]}:{t[2:4]}"
    return t


def _render_ha_chart(ticker: str, name: str, candle_data: dict):
    candles = candle_data.get("candles", [])
    if not candles:
        st.caption(f"{ticker} 데이터 없음")
        return
    display_name = name or candle_data.get("name", "") or ticker
    vwap = candle_data.get("vwap", 0)
    dev_pct = candle_data.get("vwap_deviation_pct", 0)
    times = [_fmt_time(c.get("time", "")) or str(j) for j, c in enumerate(candles)]
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=times,
        open=[c["ha_open"] for c in candles],
        high=[c["ha_high"] for c in candles],
        low=[c["ha_low"] for c in candles],
        close=[c["ha_close"] for c in candles],
        name="HA",
        increasing_line_color="#2ecc71",
        decreasing_line_color="#e74c3c",
        increasing_fillcolor="#2ecc71",
        decreasing_fillcolor="#e74c3c",
    ))
    if vwap:
        fig.add_hline(
            y=vwap, line_color="orange", line_dash="dash",
            annotation_text=f"VWAP {vwap:,} ({dev_pct:+.1f}%)",
            annotation_position="bottom right",
        )
    fig.update_layout(
        title=f"{display_name} ({ticker}) 3분봉 HA",
        height=240,
        margin=dict(t=35, b=20, l=45, r=20),
        xaxis_rangeslider_visible=False,
        xaxis_tickangle=-45,
        xaxis=dict(type="category"),
    )
    st.plotly_chart(fig, use_container_width=True)


if refresh:
    st.cache_data.clear()

# ── 시뮬레이션 저장소 헬퍼 (모듈 레벨 — 모든 페이지에서 사용) ──────────
_SIM_DIR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "simulations")


def _local_sim_path(filename: str) -> str:
    os.makedirs(_SIM_DIR_PATH, exist_ok=True)
    return os.path.join(_SIM_DIR_PATH, filename)


def _sim_load_index() -> list:
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob("simulations/index.json")
            if blob.exists():
                idx = json.loads(blob.download_as_text())
            else:
                idx = []
        except Exception:
            return []
    else:
        try:
            with open(_local_sim_path("index.json"), encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:
            idx = []
    # 20분 이상 running 상태인 항목 자동 만료 (컨테이너 재시작 등으로 스레드 소멸 시 대비)
    _now = get_now_kst()
    _changed = False
    for _e in idx:
        if _e.get("status") != "running":
            continue
        try:
            _dt = datetime.fromisoformat(_e["created_at"])
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=KST)
            if (_now - _dt).total_seconds() > 1200:
                _e["status"] = "error"
                _e["finished_at"] = _now.isoformat()
                _changed = True
        except Exception:
            _e["status"] = "error"
            _e["finished_at"] = _now.isoformat()
            _changed = True
    if _changed:
        _sim_save_index(idx)
    return idx


def _sim_save_index(index: list):
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob("simulations/index.json")
            blob.upload_from_string(json.dumps(index, ensure_ascii=False), content_type="application/json")
            return
        except Exception:
            pass
    try:
        with open(_local_sim_path("index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
    except Exception:
        pass


def _sim_load_data(sim_id: str) -> dict:
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(f"simulations/{sim_id}.json")
            if blob.exists():
                return json.loads(blob.download_as_text())
        except Exception:
            pass
        return {}
    try:
        with open(_local_sim_path(f"{sim_id}.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sim_save_data(sim_id: str, data: dict):
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(f"simulations/{sim_id}.json")
            blob.upload_from_string(json.dumps(data, ensure_ascii=False), content_type="application/json")
            return
        except Exception:
            pass
    try:
        with open(_local_sim_path(f"{sim_id}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _sim_delete(sim_id: str):
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            storage.Client().bucket(_GCS_DATA_BUCKET).blob(f"simulations/{sim_id}.json").delete()
        except Exception:
            pass
    else:
        try:
            os.remove(_local_sim_path(f"{sim_id}.json"))
        except Exception:
            pass
    _sim_save_index([x for x in _sim_load_index() if x["id"] != sim_id])


_PROGRESS_BLOB = "session_progress.json"
_PROGRESS_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "session_progress.json")


def _load_session_progress() -> dict:
    """GCS 또는 로컬에서 세션 진행상황 읽기 (스케줄/대시보드 공용)."""
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(_PROGRESS_BLOB)
            data = json.loads(blob.download_as_text())
            if data.get("status") in ("running", "summarizing", "llm_running"):
                try:
                    started = datetime.fromisoformat(data["started_at"])
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=KST)
                    if (get_now_kst() - started).total_seconds() > 1200:
                        return {}
                except Exception:
                    return {}
            return data
        except Exception as e:
            if "404" not in str(e) and "NotFound" not in type(e).__name__:
                logger.warning("session_progress GCS 읽기 실패 — 로컬 폴백: %s", e)
    try:
        with open(_PROGRESS_LOCAL, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    return {}
    try:
        with open(_PROGRESS_LOCAL, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_session_progress(data: dict):
    """대시보드 실행 진행상황을 GCS/로컬에 기록."""
    try:
        os.makedirs(os.path.dirname(_PROGRESS_LOCAL), exist_ok=True)
        with open(_PROGRESS_LOCAL, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            storage.Client().bucket(_GCS_DATA_BUCKET).blob(_PROGRESS_BLOB).upload_from_string(
                json.dumps(data, ensure_ascii=False), content_type="application/json"
            )
        except Exception as e:
            logger.warning("session_progress GCS 쓰기 실패: %s", e)


def _run_agent_bg(sim_id: str, live: bool):
    """백그라운드 스레드: 5루프 에이전트 실행, 루프마다 진행상황 기록."""
    from config import INNER_LOOP_COUNT, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS
    from agent.trader import TradingAgent
    from agent.tools import _broker
    from data.trade_log import log_agent_run

    if not live:
        os.environ["DRY_RUN"] = "true"

    now = get_now_kst()
    progress: dict = {
        "session_id": sim_id,
        "source": "dashboard",
        "mode": "dry_run" if not live else "live",
        "status": "running",
        "started_at": now.isoformat(),
        "total_loops": INNER_LOOP_COUNT,
        "current_loop": 0,
        "loops": [],
    }
    _write_session_progress(progress)

    data: dict = {"id": sim_id, "status": "running", "created_at": now.isoformat(), "loops": []}
    _sim_save_data(sim_id, data)

    try:
        agent = TradingAgent()
        broker = _broker()
        session_log: list[dict] = []
        all_buy_tickers: list[str] = []

        for i in range(INNER_LOOP_COUNT):
            loop_entry: dict = {"loop": i + 1, "ha_signals": [], "result": None, "tool_log": []}
            loop_p: dict = {"loop": i + 1, "status": "checking", "ha_signals": [], "needs_action": False}
            progress["current_loop"] = i + 1
            progress["loops"].append(loop_p)
            _write_session_progress(progress)

            if not live:
                needs_action = True
            else:
                try:
                    needs_action, ha_signals = _check_needs_action_dashboard(
                        broker, TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_POSITIONS,
                    )
                    loop_entry["ha_signals"] = ha_signals
                    loop_p["ha_signals"] = ha_signals
                except Exception:
                    needs_action = True

            loop_p["needs_action"] = needs_action
            if needs_action:
                loop_p["status"] = "llm_running"
                _write_session_progress(progress)

                def _on_tool_call(tool_log, _lp=loop_p, _le=loop_entry):
                    _lp["tool_log"] = tool_log[-10:]
                    _le["tool_log"] = tool_log
                    _write_session_progress(progress)
                    _sim_save_data(sim_id, data)

                result = agent.run(
                    cancel_pending=(i == 0),
                    skip_log=True,
                    sim_datetime=_get_sim_datetime(live),
                    on_tool_call=_on_tool_call,
                )
                loop_entry["result"] = result
                loop_p["result_preview"] = (result or "")[:300]
                all_buy_tickers.extend(agent.buy_tickers)

            loop_p["status"] = "done"
            data["loops"].append(loop_entry)
            _write_session_progress(progress)
            _sim_save_data(sim_id, data)
            session_log.append(loop_entry)

            if i < INNER_LOOP_COUNT - 1:
                import time as _t
                _t.sleep(5)

        # 세션 최종 요약
        progress["status"] = "summarizing"
        _write_session_progress(progress)
        final_summary = agent.summarize_session(session_log)
        data["final_summary"] = final_summary
        try:
            portfolio_snapshot = broker.get_balance()
        except Exception:
            portfolio_snapshot = {}
        log_agent_run(None, final_summary, portfolio_snapshot,
                      list(dict.fromkeys(all_buy_tickers)), session_log)

    except Exception as e:
        data["error"] = str(e)
        progress["status"] = "done"
    finally:
        os.environ["DRY_RUN"] = "false"
        finished_at = get_now_kst().isoformat()
        data["status"] = "done"
        data["finished_at"] = finished_at
        progress["status"] = "done"
        progress["finished_at"] = finished_at
        _sim_save_data(sim_id, data)
        _write_session_progress(progress)
        idx = _sim_load_index()
        entry = next((x for x in idx if x["id"] == sim_id), None)
        if entry:
            entry["status"] = "done"
            entry["finished_at"] = finished_at
        _sim_save_index(idx)


def _check_needs_action_dashboard(broker, take_profit_pct, stop_loss_pct, max_positions):
    """대시보드 백그라운드 스레드용 사전 체크 (main._check_needs_action과 동일 로직)."""
    portfolio = broker.get_balance()
    holdings = portfolio.get("holdings", [])
    ha_signals = []

    for h in holdings:
        rate = h.get("profit_loss_rate", 0)
        if rate >= take_profit_pct or rate <= -stop_loss_pct:
            return True, ha_signals

    if len(holdings) < max_positions:
        return True, ha_signals

    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        try:
            result = broker.get_minute_candles(ticker)
            candles = result.get("candles", [])
            vwap_dev = result.get("vwap_deviation_pct", 0)
            if not candles:
                continue
            latest = candles[-1]
            signal = {
                "ticker": ticker,
                "name": result.get("name", ""),
                "pattern": latest.get("pattern", ""),
                "vwap_dev": round(float(vwap_dev), 2),
                "bullish": latest.get("bullish", True),
            }
            ha_signals.append(signal)
            if vwap_dev < 0:
                return True, ha_signals
        except Exception:
            pass
    return False, ha_signals


def _launch_agent(live: bool) -> str | None:
    """에이전트를 백그라운드로 실행하고 sim_id 반환. 이미 실행 중이면 None."""
    _existing = _load_session_progress()
    if _existing.get("status") in ("running", "summarizing"):
        return None
    _now = get_now_kst()
    sim_id = _now.strftime("%Y%m%d_%H%M%S")
    mode_str = "실전" if live else "시뮬"
    _sim_save_data(sim_id, {
        "id": sim_id, "status": "running",
        "mode": mode_str, "created_at": _now.isoformat(), "results": {},
    })
    idx = _sim_load_index()
    idx.insert(0, {
        "id": sim_id, "created_at": _now.isoformat(),
        "mode": mode_str, "status": "running", "finished_at": None,
    })
    _sim_save_index(idx)
    # 스레드 시작 전에 progress를 먼저 기록 — st.rerun() 후 첫 렌더링에서
    # _prog_active=True 가 되어 자동 새로고침 루프가 즉시 시작됨
    _write_session_progress({
        "session_id": sim_id,
        "source": "dashboard",
        "mode": "live" if live else "dry_run",
        "status": "running",
        "started_at": _now.isoformat(),
        "total_loops": 1,
        "current_loop": 0,
        "loops": [],
    })
    threading.Thread(target=_run_agent_bg, args=(sim_id, live), daemon=True).start()
    return sim_id



# ════════════════════════════════════════════════════════
if page == "포트폴리오":
    st.title("포트폴리오 현황")

    data = load_balance()

    if "error" in data:
        st.error(f"잔고 조회 실패: {data['error']}")
        with st.expander("🔍 진단 정보"):
            from config import KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_MOCK, KIS_BASE_URL
            st.json({
                "KIS_APP_KEY":    "✅ 설정됨" if KIS_APP_KEY    else "❌ 미설정",
                "KIS_APP_SECRET": "✅ 설정됨" if KIS_APP_SECRET else "❌ 미설정",
                "KIS_ACCOUNT_NO": "✅ 설정됨" if KIS_ACCOUNT_NO else "❌ 미설정",
                "KIS_MOCK":       KIS_MOCK,
                "KIS_BASE_URL":   KIS_BASE_URL,
                "GCS_DATA_BUCKET": "✅ 설정됨" if _GCS_DATA_BUCKET else "❌ 미설정",
            })
        st.stop()

    # 상단 요약 카드
    col1, col2, col3, col4, col5 = st.columns(5)
    available_cash = data.get("cash", 0)          # dnca_tot_amt: 예수금 (주식 매수 가능한 현금)
    holdings_eval = data.get("holdings_eval", 0)  # scts_evlu_amt: 보유 주식 평가금액
    holdings = data.get("holdings", [])
    total_eval = available_cash + holdings_eval    # 평가금액 = 예수금 + 주식평가

    # 투자금액: 대시보드 설정 → 환경변수 → 예수금 순 fallback
    INITIAL_CAPITAL = _get_initial_capital()
    invested = INITIAL_CAPITAL if INITIAL_CAPITAL > 0 else available_cash
    profit_loss = total_eval - invested
    pl_rate = round((profit_loss / invested * 100), 2) if invested > 0 else 0.0

    col1.metric("투자금액", f"₩{invested:,.0f}")
    col2.metric("예수금", f"₩{available_cash:,.0f}")
    col3.metric("평가금액", f"₩{total_eval:,.0f}")
    col4.metric("평가손익", f"₩{profit_loss:,.0f}", f"{pl_rate:+.2f}%",
                delta_color="normal" if profit_loss >= 0 else "inverse")
    col5.metric("보유 종목 수", f"{len(holdings)}개")

    # ── 보유 종목 (메트릭 카드 바로 아래) ─────────────────────────────
    if holdings:
        st.divider()
        st.subheader("📌 보유 종목")
        _hold_rows = []
        for _h in holdings:
            _rate    = _h.get("profit_loss_rate", 0)
            _avg_p   = _h.get("avg_price", 0)
            _qty     = _h.get("quantity", 0)
            _cur_p   = _h.get("current_price", 0)
            _buy_amt = _avg_p * _qty
            _pl_amt  = (_cur_p - _avg_p) * _qty

            if _rate >= 3.5:    _remark = "🎯 TP 임박"
            elif _rate >= 1.5:  _remark = "📈 수익권"
            elif _rate >= 0:    _remark = "🟡 보합"
            elif _rate >= -2.0: _remark = "📉 손실권"
            else:               _remark = "⚠️ SL 위험"

            _hold_rows.append({
                "종목명":   _h.get("name") or _h.get("ticker", ""),
                "코드":     _h.get("ticker", ""),
                "수량":     _qty,
                "매수가":   _avg_p,
                "현재가":   _cur_p,
                "수익률":   _rate,
                "매수금액": _buy_amt,
                "손익":     _pl_amt,
                "비고":     _remark,
            })

        _hdf = pd.DataFrame(_hold_rows)
        st.dataframe(
            _hdf.style
                .map(lambda v: "color: #2ecc71" if v >= 0 else "color: #e74c3c",
                     subset=["수익률", "손익"])
                .format({
                    "매수가":   "{:,.0f}",
                    "현재가":   "{:,.0f}",
                    "수익률":   "{:+.2f}%",
                    "매수금액": "₩{:,.0f}",
                    "손익":     "₩{:+,.0f}",
                }),
            use_container_width=True,
            hide_index=True,
        )

    # ── 에이전트 상태 / 실행 버튼 ──────────────────────────────────────
    import time as _time
    import holidays as _hol
    from datetime import time as _dtime

    _prog = _load_session_progress()
    _prog_active = _prog.get("status") in ("running", "summarizing")

    st.divider()

    if _prog_active:
        _cur = _prog.get("current_loop", 0)
        _tot = _prog.get("total_loops", 5)
        _src = _prog.get("source", "scheduled")
        _mode_label = "🔴 실전" if _prog.get("mode") == "live" else "🧪 시뮬/DRY"
        _src_label = "대시보드" if _src == "dashboard" else "스케줄"
        _is_summ = _prog.get("status") == "summarizing"

        with st.container(border=True):
            _hcol1, _hcol2 = st.columns([3, 1])
            _hcol1.markdown(
                f"**⏳ 에이전트 실행 중 — {_mode_label} ({_src_label})**",
                unsafe_allow_html=True,
            )
            if _is_summ:
                _hcol2.caption("📝 최종 요약 중...")
            else:
                _hcol2.caption(f"루프 {_cur}/{_tot}")

            if not _is_summ and _tot > 0:
                st.progress(_cur / _tot, text=f"루프 {_cur} / {_tot}")

            _loops = _prog.get("loops", [])
            for _lp in _loops:
                _lnum = _lp["loop"]
                _lst = _lp.get("status", "")
                _lna = _lp.get("needs_action", False)
                _lha = _lp.get("ha_signals", [])
                _ltl = _lp.get("tool_log", [])

                if _lst == "done":
                    _icon = "✅"
                elif _lst == "llm_running":
                    _icon = "🤖"
                elif _lst == "checking":
                    _icon = "🔍"
                else:
                    _icon = "⬜"

                _ha_str = ""
                if _lha:
                    _ha_str = " | ".join(
                        f"{s['ticker']} HA={s['pattern']} VWAP={s['vwap_dev']:+.1f}%"
                        for s in _lha
                    )

                _action_str = ""
                if _lst == "done":
                    _action_str = "LLM 호출" if _lna else "스킵"
                elif _lst == "llm_running":
                    _last_tool = _ltl[-1] if _ltl else None
                    if _last_tool:
                        _targs = ", ".join(f"{k}={v}" for k, v in _last_tool["args"].items()) if _last_tool["args"] else ""
                        _action_str = f"R{_last_tool['round']} → `{_last_tool['tool']}({_targs})`"
                    else:
                        _action_str = "Gemini 연결 중..."

                st.caption(f"{_icon} 루프 {_lnum}: {_action_str}  {_ha_str}")

                if _ltl and _lst == "llm_running":
                    with st.expander(f"📡 루프 {_lnum} 실시간 호출 ({len(_ltl)}회)", expanded=True):
                        for _te in reversed(_ltl[-6:]):
                            _ta = ", ".join(f"{k}={v}" for k, v in _te["args"].items()) if _te["args"] else ""
                            st.markdown(f"**[R{_te['round']}]** `{_te['tool']}({_ta})`")
                            st.code(_te["result_preview"], language="json")

        _time.sleep(3)
        st.rerun()

    _now_kst = get_now_kst()
    _live = (
        _now_kst.weekday() < 5 and
        _now_kst.date() not in _hol.SouthKorea(years=[_now_kst.year]) and
        _dtime(9, 0) <= _now_kst.time() <= _dtime(15, 30)
    )
    with st.expander("▶ 에이전트 실행", expanded=False):
        if _prog_active:
            _block_src = "스케줄" if _prog.get("source") == "scheduled" else "대시보드"
            _block_loop = _prog.get("current_loop", 0)
            _block_tot = _prog.get("total_loops", 5)
            st.warning(
                f"⚠️ 에이전트가 이미 실행 중입니다 ({_block_src} — 루프 {_block_loop}/{_block_tot})\n\n"
                "현재 실행이 완료될 때까지 새로운 실행을 시작할 수 없습니다."
            )
        else:
            _pw = st.text_input("비밀번호", type="password", key="portfolio_run_pw")
            if _pw and _pw != "1018":
                st.error("비밀번호가 올바르지 않습니다.")
            elif _pw == "1018":
                if _live:
                    st.error("🔴 장중 — 실제 계좌로 실제 주문이 실행됩니다")
                    _run_lbl = "📈 실전 실행"
                else:
                    st.info("🧪 장외 — 가상 ₩1,000,000 시뮬레이션")
                    _run_lbl = "🧪 시뮬 실행"
                if st.button(_run_lbl, type="primary", key="portfolio_run_btn"):
                    _sid = _launch_agent(_live)
                    if _sid is None:
                        st.warning("⚠️ 이미 에이전트가 실행 중입니다.")
                    else:
                        st.rerun()

    # ── 최근 실행 이력 (스케줄 + 수동 전체, agent_runs.jsonl 기반) ────────────
    _recent_agent_runs = get_agent_runs(limit=5)
    if _recent_agent_runs:
        st.subheader("최근 실행 이력")
        for _run in reversed(_recent_agent_runs):
            _rts = _run.get("ts", "")
            try:
                _rdt = pd.to_datetime(_rts, utc=True).tz_convert(KST)
                _rts_disp = _rdt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                _rts_disp = _rts[:16].replace("T", " ")
            _rpf = _run.get("portfolio", {})
            _rte = _rpf.get("total_eval", 0) or 0
            _rbuy = _run.get("buy_tickers", [])
            _rbuy_str = f"  |  매수: {', '.join(_rbuy)}" if _rbuy else ""
            _rsum = _run.get("summary", "")
            _rprev = ""
            for _rln in _rsum.split('\n'):
                _rln = _rln.strip().lstrip('#-| ').strip()
                if _rln and not _rln.startswith('---'):
                    _rprev = _rln[:40] + ('…' if len(_rln) > 40 else '')
                    break
            with st.expander(f"🤖 {_rts_disp} — ₩{_rte:,.0f}{_rbuy_str}  {_rprev}"):
                _rc1, _rc2, _rc3 = st.columns(3)
                _rc1.metric("예수금", f"₩{_rpf.get('cash', 0):,.0f}")
                _rc2.metric("평가금액", f"₩{_rte:,.0f}")
                _rc3.metric("보유 종목", f"{len(_rpf.get('holdings', []))}개")
                if _rsum:
                    st.markdown(_rsum)

    st.divider()

    # 손익률 추이 차트
    st.subheader("손익률 추이")
    _period_col, _ = st.columns([1, 3])
    with _period_col:
        _days = st.selectbox("기간", [5, 10, 20, 30], index=1, format_func=lambda x: f"최근 {x}일", label_visibility="collapsed")

    _runs = get_agent_runs(limit=500)
    if _runs:
        _ic = _get_initial_capital()
        def _run_pl_rate(r):
            p = r.get("portfolio", {})
            _te = p.get("total_eval", 0) or 0
            if _ic > 0:
                return round((_te - _ic) / _ic * 100, 2)
            _pl = p.get("profit_loss", 0) or 0
            _hs = p.get("holdings", []) or []
            _he = sum(h.get("current_price", 0) * h.get("quantity", 0) for h in _hs)
            _se = _he if _he > 0 else _te
            _cb = _se - _pl
            return round((_pl / _cb * 100), 2) if _cb > 0 else 0.0

        _rdf = pd.DataFrame([{"ts": r["ts"], "pl_rate": _run_pl_rate(r)} for r in _runs])
        _rdf["ts"] = pd.to_datetime(_rdf["ts"], format='ISO8601', utc=True).dt.tz_convert(KST)
        _rdf["date"] = _rdf["ts"].dt.date
        _daily = _rdf.sort_values("ts").groupby("date")["pl_rate"].last().reset_index()
        _cutoff = (get_now_kst() - timedelta(days=_days)).date()
        _daily = _daily[_daily["date"] >= _cutoff]

        if not _daily.empty:
            _colors = ["#e74c3c" if v < 0 else "#2ecc71" for v in _daily["pl_rate"]]
            _fig_pl = go.Figure()
            _fig_pl.add_trace(go.Bar(
                x=_daily["date"], y=_daily["pl_rate"],
                marker_color=_colors,
                text=[f"{v:+.2f}%" for v in _daily["pl_rate"]],
                textposition="outside",
            ))
            _fig_pl.add_hline(y=0, line_color="gray", line_width=1)
            _fig_pl.update_layout(
                yaxis_title="손익률 (%)",
                height=260, margin=dict(t=10, b=40, l=40, r=20),
            )
            st.plotly_chart(_fig_pl, use_container_width=True)
        else:
            st.info("해당 기간의 에이전트 실행 데이터가 없습니다.")
    else:
        st.info("에이전트 실행 기록이 없어 손익률 추이를 표시할 수 없습니다.")

    st.divider()

    if holdings:
        # 포트폴리오 비중 차트
        col_chart1, col_chart2 = st.columns(2)
        with col_chart1:
            st.subheader("종목별 비중")
            labels = [f"{h.get('name', h['ticker'])}" for h in holdings]
            values = [h["current_price"] * h["quantity"] for h in holdings]
            labels.append("예수금")
            values.append(available_cash)
            fig = px.pie(names=labels, values=values, hole=0.4)
            fig.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=300)
            st.plotly_chart(fig, use_container_width=True)

        with col_chart2:
            st.subheader("종목별 수익률")
            names = [h.get("name", h["ticker"]) for h in holdings]
            rates = [h["profit_loss_rate"] for h in holdings]
            colors = ["#2ecc71" if r >= 0 else "#e74c3c" for r in rates]
            fig2 = go.Figure(go.Bar(x=names, y=rates, marker_color=colors, text=[f"{r:+.2f}%" for r in rates], textposition="outside"))
            fig2.update_layout(yaxis_title="수익률 (%)", margin=dict(t=20, b=20, l=20, r=20), height=300)
            st.plotly_chart(fig2, use_container_width=True)

    # ── 미체결 주문 ──────────────────
    st.divider()
    st.subheader("⏳ 미체결 주문")
    try:
        pending = _get_kis_broker().get_pending_orders()
    except Exception as e:
        pending = None
        st.warning(f"미체결 조회 실패: {e}")

    if pending is not None:
        if not pending:
            st.info("미체결 주문 없음")
        else:
            pdf = pd.DataFrame(pending)
            pdf["구분"] = pdf["action"].apply(lambda x: "🟢 매수" if x == "BUY" else "🔴 매도")
            pdf["주문가"] = pdf["order_price"].apply(lambda x: f"₩{x:,}" if x else "시장가")
            display_pdf = pdf[["구분", "ticker", "name", "order_qty", "filled_qty", "remaining_qty", "주문가", "order_type"]].copy()
            display_pdf.columns = ["구분", "종목코드", "종목명", "주문수량", "체결수량", "미체결수량", "주문가", "주문유형"]
            st.dataframe(display_pdf, use_container_width=True, hide_index=True)

# ════════════════════════════════════════════════════════
elif page == "매매 이력":
    st.title("매매 이력")

    tab_kis, tab_agent = st.tabs(["📋 KIS 계좌 체결 이력", "🤖 에이전트 주문 로그"])

    # ── KIS 실계좌 체결 이력 ──────────
    with tab_kis:
        col_date1, col_date2, col_btn = st.columns([2, 2, 1])
        with col_date1:
            start_d = st.date_input("시작일", value=get_now_kst().date() - timedelta(days=30))
        with col_date2:
            end_d = st.date_input("종료일", value=get_now_kst().date())
        with col_btn:
            st.write("")
            fetch_btn = st.button("조회", use_container_width=True)

        @st.cache_data(ttl=60)
        def load_order_history(start: str, end: str):
            try:
                return _get_kis_broker().get_order_history(start, end)
            except Exception as e:
                return {"error": str(e)}

        if fetch_btn or "kis_history" not in st.session_state:
            st.session_state["kis_history"] = load_order_history(
                start_d.strftime("%Y%m%d"), end_d.strftime("%Y%m%d")
            )

        history = st.session_state.get("kis_history", [])

        if isinstance(history, dict) and "error" in history:
            st.error("KIS 체결 이력 조회에 실패했습니다. API 설정을 확인하세요.")
        elif not history:
            st.info("해당 기간에 체결된 주문이 없습니다.")
        else:
            hdf = pd.DataFrame(history)
            hdf["ts"] = pd.to_datetime(hdf["ts"], format="%Y%m%d %H%M%S", errors="coerce")

            col1, col2, col3 = st.columns(3)
            col1.metric("성 체결", len(hdf))
            col2.metric("매수", len(hdf[hdf["action"] == "BUY"]))
            col3.metric("매도", len(hdf[hdf["action"] == "SELL"]))

            st.divider()

            disp = hdf[["ts", "action", "ticker", "name", "quantity", "price", "amount"]].copy()
            disp.columns = ["일시", "구분", "종목코드", "종목명", "수량", "체결가", "금액"]
            disp["일시"] = disp["일시"].dt.strftime("%Y-%m-%d %H:%M:%S")
            disp["체결가"] = disp["체결가"].apply(lambda x: f"₩{x:,.0f}")
            disp["금액"] = disp["금액"].apply(lambda x: f"₩{x:,.0f}")
            disp["구분"] = disp["구분"].apply(lambda x: "🟢 매수" if x == "BUY" else "🔴 매도")
            st.dataframe(disp, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("일별 매매 금액")
            hdf["date"] = hdf["ts"].dt.date
            daily = hdf.groupby(["date", "action"])["amount"].sum().reset_index()
            fig = px.bar(daily, x="date", y="amount", color="action",
                         color_discrete_map={"BUY": "#2ecc71", "SELL": "#e74c3c"},
                         labels={"date": "날짜", "amount": "금액 (원)", "action": "구분"})
            fig.update_layout(height=300, margin=dict(t=20, b=20, l=20, r=20))
            st.plotly_chart(fig, use_container_width=True)

    # ── 에이전트 주문 로그 ──────────
    with tab_agent:
        trades = get_trades()
        if not trades:
            st.info("아직 에이전트 주문 기록이 없습니다. 에이전트를 실행하면 여기에 기록됩니다.")
        else:
            df = pd.DataFrame(trades)
            # 타임존 정보가 있으면 KST로 변환, 없으면 KST로 지정
            df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(KST)
            df = df.sort_values("ts", ascending=False)

            col1, col2, col3 = st.columns(3)
            col1.metric("성 주문", len(df))
            col2.metric("매수", len(df[df["action"] == "BUY"]))
            col3.metric("매도", len(df[df["action"] == "SELL"]))

            st.divider()

            if "name" not in df.columns:
                df["name"] = ""
            if "profit" not in df.columns:
                df["profit"] = None
            if "vwap_dev" not in df.columns:
                df["vwap_dev"] = None
            if "ha_pattern" not in df.columns:
                df["ha_pattern"] = ""
            # 종목명: name이 비어있거나 ticker와 같으면 ticker 표시
            df["종목명"] = df.apply(
                lambda r: r["name"] if r["name"] and r["name"] != r["ticker"] else r["ticker"], axis=1
            )
            # VWAP 이탈률 표시
            df["VWAP%"] = df["vwap_dev"].apply(
                lambda v: f"{v:+.1f}%" if v is not None and not (isinstance(v, float) and v != v) else "-"
            )
            # 실현손익: SELL 행만 표시
            df["실현손익"] = df.apply(
                lambda r: f"{'▲' if r['profit'] > 0 else '▼'} ₩{abs(r['profit']):,.0f}"
                if r["action"] == "SELL" and r["profit"] is not None and r["profit"] != 0
                else ("" if r["action"] == "BUY" else "-"),
                axis=1,
            )
            display_df = df[["ts", "action", "ticker", "종목명", "VWAP%", "ha_pattern", "실현손익", "quantity", "price", "amount", "success", "reason"]].copy()
            display_df.columns = ["일시", "구분", "종목코드", "종목명", "VWAP%", "HA패턴", "실현손익", "수량", "체결가", "금액", "성공", "판단 근거"]
            display_df["일시"] = display_df["일시"].dt.strftime("%Y-%m-%d %H:%M")
            display_df["체결가"] = display_df["체결가"].apply(lambda x: f"₩{x:,.0f}")
            display_df["금액"] = display_df["금액"].apply(lambda x: f"₩{x:,.0f}")
            display_df["구분"] = display_df["구분"].apply(
                lambda x: "🟢 매수" if x == "BUY" else ("⬜ 취소" if x == "CANCEL" else "🔴 매도")
            )
            st.dataframe(display_df, use_container_width=True, hide_index=True)

            # ── 다운로드 ──────────────────────────────────────
            st.divider()
            _dl1, _dl2 = st.columns(2)

            # CSV 다운로드
            _csv_cols = ["ts", "action", "ticker", "name", "vwap_dev", "ha_pattern",
                         "quantity", "price", "amount", "profit", "success", "reason"]
            _export_df = df[[c for c in _csv_cols if c in df.columns]].copy()
            _export_df["ts"] = _export_df["ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
            _dl1.download_button(
                label="📥 CSV 다운로드",
                data=_export_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"trades_{get_now_kst().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )

            # JSONL 다운로드 (원본 — Claude 분석용)
            import json as _json
            _raw_lines = "\n".join(_json.dumps(r, ensure_ascii=False) for r in trades)
            _dl2.download_button(
                label="📥 JSONL 다운로드 (Claude 분석용)",
                data=_raw_lines.encode("utf-8"),
                file_name=f"trades_{get_now_kst().strftime('%Y%m%d')}.jsonl",
                mime="application/jsonlines",
            )
            st.caption("💡 JSONL 파일을 `logs/trades.jsonl`에 저장하면 Claude Code가 바로 분석합니다.")

# ════════════════════════════════════════════════════════
elif page == "에이전트 로그":
    def _summary_preview(text: str, max_len: int = 55) -> str:
        for line in text.split('\n'):
            line = line.strip().lstrip('#-| ').strip()
            if line and not line.startswith('---'):
                return line[:max_len] + ('…' if len(line) > max_len else '')
        return "내용 없음"

    st.title("에이전트 실행 로그")

    runs = get_agent_runs()
    if not runs:
        st.info("아직 에이전트 실행 기록이 없습니다. `python main.py --once` 로 실행해보세요.")
        st.stop()

    runs_reversed = list(reversed(runs))

    for i, run in enumerate(runs_reversed[:20]):
        raw_ts = run.get("ts", "")
        try:
            dt_ts = pd.to_datetime(raw_ts, utc=True).tz_convert(KST)
            ts_str = dt_ts.strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts_str = raw_ts[:16]

        watchlist = run.get("watchlist", [])
        summary = run.get("summary", "")
        portfolio = run.get("portfolio", {})

        cash = portfolio.get("cash", 0)
        total_eval = portfolio.get("total_eval", 0)
        holdings_count = len(portfolio.get("holdings", []))

        buy_tickers = run.get("buy_tickers", [])
        # 종목명 조회: 포트폴리오 보유 종목에서 우선 참조
        _hmap = {h.get("ticker"): h.get("name", "") for h in portfolio.get("holdings", [])}
        _buy_names = [_hmap.get(t) or t for t in buy_tickers]
        buy_badge = f"  |  매수: {', '.join(_buy_names)}" if buy_tickers else ""
        label = f"🤖 {ts_str}  |  {_summary_preview(summary)}  |  잔고: ₩{total_eval:,.0f}{buy_badge}"
        with st.expander(label, expanded=(i == 0)):
            col1, col2, col3 = st.columns(3)
            col1.metric("예수금", f"₩{cash:,.0f}")
            col2.metric("성 평가금액", f"₩{total_eval:,.0f}")
            col3.metric("보유 종목", f"{holdings_count}개")

            if buy_tickers:
                st.markdown("**매수 종목 3분봉 HA 차트**")
                try:
                    is_today = dt_ts.date() == get_now_kst().date()
                    if is_today:
                        chart_cols = st.columns(min(len(buy_tickers), 2))
                        for j, ticker in enumerate(buy_tickers):
                            with chart_cols[j % 2]:
                                candle_data = _fetch_candles(ticker)
                                _render_ha_chart(ticker, _hmap.get(ticker, ""), candle_data)
                    else:
                        st.caption(f"매수 종목: {', '.join(_buy_names)}  (과거 데이터 — 차트 생략)")
                except Exception as e:
                    st.caption(f"차트 조회 오류: {e}")

            st.markdown("**에이전트 판단 요약**")
            st.markdown(summary)

