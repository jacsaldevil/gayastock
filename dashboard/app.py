"""gayastock 대시보드 — streamlit run dashboard/app.py"""
import sys
import os
import json
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def _load_settings() -> dict:
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(_SETTINGS_BLOB)
            if blob.exists():
                return json.loads(blob.download_as_text())
        except Exception:
            pass
    # 로챈 fallback
    try:
        with open(".dashboard_settings.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(data: dict):
    if _GCS_DATA_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(_SETTINGS_BLOB)
            blob.upload_from_string(json.dumps(data), content_type="application/json")
            return
        except Exception:
            pass
    try:
        with open(".dashboard_settings.json", "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _get_initial_capital() -> int:
    """GCS 설정 → 환경변수 순으로 투자금액 반환"""
    settings = _load_settings()
    val = settings.get("initial_capital", 0)
    if val > 0:
        return val
    return _INITIAL_CAPITAL_ENV


def _get_sim_datetime(live: bool):
    """장외 시뮬 실행 시 1회차 조건 강제, 장중이면 None(실제 시각)"""
    if live:
        return None
    from agent.trader import _SCHEDULE_SLOTS
    from datetime import datetime as _dt, time as _dtime
    now_kst = get_now_kst()
    if _dtime(9, 0) <= now_kst.time() <= _dtime(15, 30):
        return None
    first_slot = _SCHEDULE_SLOTS[0][0]
    return _dt(now_kst.year, now_kst.month, now_kst.day,
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
_cur_capital = _get_initial_capital()
_new_capital = st.sidebar.number_input(
    "투자 원금 (원)", min_value=0, step=10000,
    value=_cur_capital, format="%d",
    help="0이면 현재 예수금 기준으로 표시됩니다.",
    label_visibility="collapsed",
)
if st.sidebar.button("저장", use_container_width=True):
    _save_settings({"initial_capital": int(_new_capital)})
    st.cache_data.clear()
    st.rerun()
if not _GCS_DATA_BUCKET:
    st.sidebar.caption("⚠️ GCS 미설정 — 재배포 시 초기화됨")

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


def _run_agent_bg(sim_id: str, live: bool):
    """백그라운드 스레드: 에이전트 1회 실행, tool call마다 중간 결과 저장"""
    def _mark_done_in_index(finished_at):
        idx = _sim_load_index()
        entry = next((x for x in idx if x["id"] == sim_id), None)
        if entry:
            entry["status"] = "done"
            entry["finished_at"] = finished_at
        _sim_save_index(idx)

    if not live:
        os.environ["DRY_RUN"] = "true"
    data: dict = {
        "id": sim_id, "status": "running",
        "created_at": get_now_kst().isoformat(), "results": {},
    }
    _sim_save_data(sim_id, data)
    try:
        from agent.trader import TradingAgent
        agent = TradingAgent()

        def _on_tool_call(tool_log):
            data["results"]["분석"] = {"result": "⏳ 분석 중...", "tool_log": tool_log}
            _sim_save_data(sim_id, data)

        result = agent.run(
            [], sim_datetime=_get_sim_datetime(live),
            sim_portfolio_in=None, skip_log=False,
            on_tool_call=_on_tool_call,
        )
        data["results"]["분석"] = {"result": result, "tool_log": agent.tool_call_log}
        _sim_save_data(sim_id, data)
    except Exception as e:
        data["results"]["분석"] = {"result": f"❌ 실행 오류: {e}", "tool_log": []}
        _sim_save_data(sim_id, data)
    finally:
        os.environ["DRY_RUN"] = "false"
        data["status"] = "done"
        data["finished_at"] = get_now_kst().isoformat()
        _sim_save_data(sim_id, data)
        _mark_done_in_index(data["finished_at"])


def _launch_agent(live: bool) -> str | None:
    """에이전트를 백그라운드로 실행하고 sim_id 반환. 이미 실행 중이면 None."""
    if next((x for x in _sim_load_index() if x.get("status") == "running"), None):
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
    threading.Thread(target=_run_agent_bg, args=(sim_id, live), daemon=True).start()
    return sim_id


def _show_sim_results(sim_data: dict):
    import time as _time
    status = sim_data.get("status", "")
    finished_disp = (sim_data.get("finished_at") or "")[:16].replace("T", " ")
    results = sim_data.get("results", {})

    if status == "running":
        entry = list(results.values())[0] if results else {}
        tool_log = entry.get("tool_log", [])
        if tool_log:
            st.warning(f"⏳ 실행 중... — 도구 호출 {len(tool_log)}회 완료")
            with st.expander(f"📡 실시간 호출 로그 ({len(tool_log)}회)", expanded=True):
                for e in reversed(tool_log[-10:]):
                    args_str = ", ".join(f"{k}={v}" for k, v in e["args"].items()) if e["args"] else ""
                    st.markdown(f"**[Round {e['round']}]** `{e['tool']}({args_str})`")
                    st.code(e["result_preview"], language="json")
        else:
            st.warning("⏳ 실행 중... — Gemini 연결 중 (잠시 후 자동 갱신)")
        _time.sleep(3)
        st.rerun()
    elif status == "done":
        st.success(f"✅ 완료 — {finished_disp}")

    if not results:
        return
    entry = list(results.values())[0]
    result_text = entry.get("result", "결과 없음")
    if result_text != "⏳ 분석 중...":
        st.markdown(result_text)
    tool_log = entry.get("tool_log", [])
    if tool_log and status == "done":
        with st.expander(f"🔍 함수 호출 플로우 ({len(tool_log)}회)"):
            for e in tool_log:
                args_str = ", ".join(f"{k}={v}" for k, v in e["args"].items()) if e["args"] else ""
                st.markdown(f"**[Round {e['round']}]** `{e['tool']}({args_str})`")
                st.code(e["result_preview"], language="json")


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
    available_cash = data.get("cash", 0)   # dnca_tot_amt = 현재 예수금(가용현금)
    holdings = data.get("holdings", [])
    # 평가금액 = 예수금 + 보유주식평가 (직접 계산, KIS tot_evlu_amt는 D+2 미결제 포함으로 차이 발생)
    holding_eval = sum(h["current_price"] * h["quantity"] for h in holdings)
    total_eval = available_cash + holding_eval

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

    # ── 에이전트 상태 / 실행 버튼 ──────────────────────────────────────
    import time as _time
    import holidays as _hol
    from datetime import time as _dtime

    _idx = _sim_load_index()
    _running = next((x for x in _idx if x.get("status") == "running"), None)

    st.divider()

    if _running:
        _sim = _sim_load_data(_running["id"])
        _results = _sim.get("results", {})
        _entry = list(_results.values())[0] if _results else {}
        _tool_log = _entry.get("tool_log", [])
        _created = (_running.get("created_at") or "")[:16].replace("T", " ")
        _mode_label = "🔴 실전" if _running.get("mode") == "실전" else "🧪 시뮬"

        with st.container(border=True):
            _hcol1, _hcol2 = st.columns([3, 1])
            _hcol1.markdown(f"**⏳ 에이전트 실행 중 — {_mode_label}** &nbsp; `{_created} 시작`", unsafe_allow_html=True)
            _hcol2.caption(f"도구 호출 {len(_tool_log)}회")
            if _tool_log:
                _last = _tool_log[-1]
                _last_args = ", ".join(f"{k}={v}" for k, v in _last["args"].items()) if _last["args"] else ""
                st.caption(f"현재: Round {_last['round']} → `{_last['tool']}({_last_args})`")
                with st.expander("📡 실시간 호출 로그", expanded=True):
                    for _e in reversed(_tool_log[-8:]):
                        _a = ", ".join(f"{k}={v}" for k, v in _e["args"].items()) if _e["args"] else ""
                        st.markdown(f"**[R{_e['round']}]** `{_e['tool']}({_a})`")
                        st.code(_e["result_preview"], language="json")
            else:
                st.caption("Gemini 연결 중...")
        _time.sleep(3)
        st.rerun()
    else:
        _now_kst = get_now_kst()
        _live = (
            _now_kst.weekday() < 5 and
            _now_kst.date() not in _hol.Korea(years=[_now_kst.year]) and
            _dtime(9, 0) <= _now_kst.time() <= _dtime(15, 30)
        )
        with st.expander("▶ 에이전트 실행", expanded=False):
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

    if not holdings:
        st.info("현재 보유 종목이 없습니다.")
    else:
        # 보유 종목 테이블
        st.subheader("보유 종목")
        df = pd.DataFrame(holdings)
        df["평가금액"] = df["current_price"] * df["quantity"]
        df["매입금액"] = df["avg_price"] * df["quantity"]
        df = df.rename(columns={
            "ticker": "종목코드",
            "name": "종목명",
            "quantity": "수량",
            "avg_price": "평균단가",
            "current_price": "현재가",
            "profit_loss_rate": "수익률(%)",
        })
        display_cols = ["종목코드", "종목명", "수량", "평균단가", "현재가", "수익률(%)", "평가금액"]

        def color_pl(val):
            color = "color: #e74c3c" if val < 0 else "color: #2ecc71" if val > 0 else ""
            return color

        st.dataframe(
            df[display_cols].style.map(color_pl, subset=["수익률(%)"])  .format({
                "평균단가": "{:,.0f}",
                "현재가": "{:,.0f}",
                "수익률(%)": "{:+.2f}%",
                "평가금액": "₩{:,.0f}",
            }),
            use_container_width=True,
        )

        st.divider()

        # 포트폴리오 비중 파이차트
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
            display_df = df[["ts", "action", "ticker", "name", "quantity", "price", "amount", "success", "reason"]].copy()
            display_df.columns = ["일시", "구분", "종목코드", "종목명", "수량", "체결가", "금액", "성공", "판단 근거"]
            display_df["일시"] = display_df["일시"].dt.strftime("%Y-%m-%d %H:%M")
            display_df["체결가"] = display_df["체결가"].apply(lambda x: f"₩{x:,.0f}")
            display_df["금액"] = display_df["금액"].apply(lambda x: f"₩{x:,.0f}")
            display_df["구분"] = display_df["구분"].apply(lambda x: "🟢 매수" if x == "BUY" else "🔴 매도")
            st.dataframe(display_df, use_container_width=True, hide_index=True)

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

