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
from broker.kis import KISBroker
from data.trade_log import get_trades, get_agent_runs

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

# ── 사이드바 ──────────────────────────
st.sidebar.title("📈 gayastock")
st.sidebar.caption("AI 주식 트레이딩 에이전트")
page = st.sidebar.radio("메뉴", ["포트폴리오", "매매 이력", "에이전트 로그", "에이전트 실행"])
refresh = st.sidebar.button("🔄 새로고침")

@st.cache_data(ttl=30)
def load_balance():
    try:
        broker = KISBroker()
        return broker.get_balance()
    except Exception as e:
        return {"error": str(e)}


@st.cache_data(ttl=120)
def _fetch_candles(ticker: str) -> dict:
    try:
        return KISBroker().get_minute_candles(ticker)
    except Exception:
        return {}


def _render_ha_chart(ticker: str, name: str, candle_data: dict):
    candles = candle_data.get("candles", [])
    if not candles:
        st.caption(f"{ticker} 데이터 없음")
        return
    vwap = candle_data.get("vwap", 0)
    dev_pct = candle_data.get("vwap_deviation_pct", 0)
    times = [c.get("time", str(j)) for j, c in enumerate(candles)]
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
        title=f"{name or ticker} ({ticker}) 3분봉 HA",
        height=240,
        margin=dict(t=35, b=20, l=45, r=20),
        xaxis_rangeslider_visible=False,
        xaxis_tickangle=-45,
    )
    st.plotly_chart(fig, use_container_width=True)


if refresh:
    st.cache_data.clear()

# ════════════════════════════════════════════════════════
if page == "포트폴리오":
    st.title("포트폴리오 현황")

    data = load_balance()

    if "error" in data:
        st.error("잊고 조회에 실패했습니다. API 설정을 확인하세요.")
        st.stop()

    # 상단 요약 카드
    col1, col2, col3, col4, col5 = st.columns(5)
    invested = data.get("cash", 0)       # KIS dnca_tot_amt = 원금(총 입금액)
    total_eval = data.get("total_eval", 0)
    profit_loss = data.get("profit_loss", 0)
    holdings = data.get("holdings", [])

    holding_eval = sum(h["current_price"] * h["quantity"] for h in holdings)
    available_cash = total_eval - holding_eval   # 실제 가용 예수금
    securities_eval = holding_eval if holding_eval > 0 else total_eval
    cost_basis = securities_eval - profit_loss
    pl_rate = round((profit_loss / cost_basis * 100), 2) if cost_basis > 0 else 0.0

    col1.metric("투자금액", f"₩{invested:,.0f}")
    col2.metric("예수금", f"₩{available_cash:,.0f}")
    col3.metric("평가금액", f"₩{total_eval:,.0f}")
    col4.metric("평가손익", f"₩{profit_loss:,.0f}", f"{pl_rate:+.2f}%",
                delta_color="normal" if profit_loss >= 0 else "inverse")
    col5.metric("보유 종목 수", f"{len(holdings)}개")

    st.divider()

    # 손익률 추이 차트
    st.subheader("손익률 추이")
    _period_col, _ = st.columns([1, 3])
    with _period_col:
        _days = st.selectbox("기간", [5, 10, 20, 30], index=1, format_func=lambda x: f"최근 {x}일", label_visibility="collapsed")

    _runs = get_agent_runs(limit=500)
    if _runs:
        def _run_pl_rate(r):
            p = r.get("portfolio", {})
            _pl = p.get("profit_loss", 0) or 0
            _te = p.get("total_eval", 0) or 0
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
            df[display_cols].style.map(color_pl, subset=["수익률(%)"]).format({
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

    # ── 미체결 주문 ────────────────────
    st.divider()
    st.subheader("⏳ 미체결 주문")
    try:
        pending = KISBroker().get_pending_orders()
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

    # ── KIS 실계좌 체결 이력 ──────────────
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
                broker = KISBroker()
                return broker.get_order_history(start, end)
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
            col1.metric("총 체결", len(hdf))
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

    # ── 에이전트 주문 로그 ──────────────
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
            col1.metric("총 주문", len(df))
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
        # ISO 형식 문자열을 datetime으로 변환 후 KST로 포맷팅
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
        buy_badge = f"  |  매수: {', '.join(buy_tickers)}" if buy_tickers else ""
        label = f"🤖 {ts_str}  |  {_summary_preview(summary)}  |  잔고: ₩{total_eval:,.0f}{buy_badge}"
        with st.expander(label, expanded=(i == 0)):
            col1, col2, col3 = st.columns(3)
            col1.metric("예수금", f"₩{cash:,.0f}")
            col2.metric("총 평가금액", f"₩{total_eval:,.0f}")
            col3.metric("보유 종목", f"{holdings_count}개")

            # 매수 종목 3분봉 차트 (오늘 실행분만 라이브 조회)
            if buy_tickers:
                st.markdown("**매수 종목 3분봉 HA 차트**")
                try:
                    is_today = dt_ts.date() == get_now_kst().date()
                    if is_today:
                        chart_cols = st.columns(min(len(buy_tickers), 2))
                        for j, ticker in enumerate(buy_tickers):
                            with chart_cols[j % 2]:
                                candle_data = _fetch_candles(ticker)
                                _render_ha_chart(ticker, "", candle_data)
                    else:
                        st.caption(f"매수 종목: {', '.join(buy_tickers)}  (과거 데이터 — 차트 생략)")
                except Exception as e:
                    st.caption(f"차트 조회 오류: {e}")

            st.markdown("**에이전트 판단 요약**")
            st.markdown(summary)

# ════════════════════════════════════════════════════════
elif page == "에이전트 실행":
    st.title("📈 에이전트 실행")
    st.caption("장중: 실제 계좌 + 실제 주문 / 장외: 가상 ₩1,000,000 시뮬레이션")

    # ── 비밀번호 확인 ────────────────────
    pw = st.text_input("비밀번호", type="password", placeholder="비밀번호를 입력하세요", key="agent_pw")
    if pw and pw != "1018":
        st.error("비밀번호가 올바르지 않습니다.")
        st.stop()
    if not pw:
        st.stop()

    # ── 장중 여부 판단 ────────────────────
    import holidays as hol
    from datetime import time as dtime

    def is_market_open() -> bool:
        now = get_now_kst()
        if now.weekday() >= 5:
            return False
        if now.date() in hol.Korea(years=[now.year]):
            return False
        return dtime(9, 0) <= now.time() <= dtime(15, 30)

    LIVE = is_market_open()

    # ── 저장소 헬퍼 ────────────────────
    _GCS_DATA_BUCKET = os.environ.get("GCS_DATA_BUCKET", "")

    def _load_sim_index() -> list:
        if _GCS_DATA_BUCKET:
            try:
                from google.cloud import storage
                blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob("simulations/index.json")
                if blob.exists():
                    return json.loads(blob.download_as_text())
            except Exception:
                pass
            return []
        return st.session_state.get("sim_index", [])

    def _save_sim_index(index: list):
        if _GCS_DATA_BUCKET:
            try:
                from google.cloud import storage
                blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob("simulations/index.json")
                blob.upload_from_string(json.dumps(index, ensure_ascii=False), content_type="application/json")
            except Exception:
                pass
        else:
            st.session_state["sim_index"] = index

    def _load_sim_data(sim_id: str) -> dict:
        if _GCS_DATA_BUCKET:
            try:
                from google.cloud import storage
                blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(f"simulations/{sim_id}.json")
                if blob.exists():
                    return json.loads(blob.download_as_text())
            except Exception:
                pass
            return {}
        return st.session_state.get("sim_store", {}).get(sim_id, {})

    def _save_sim_data(sim_id: str, data: dict):
        if _GCS_DATA_BUCKET:
            try:
                from google.cloud import storage
                blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(f"simulations/{sim_id}.json")
                blob.upload_from_string(json.dumps(data, ensure_ascii=False), content_type="application/json")
            except Exception:
                pass
        else:
            st.session_state.setdefault("sim_store", {})[sim_id] = data

    def _delete_sim(sim_id: str):
        if _GCS_DATA_BUCKET:
            try:
                from google.cloud import storage
                storage.Client().bucket(_GCS_DATA_BUCKET).blob(f"simulations/{sim_id}.json").delete()
            except Exception:
                pass
        else:
            st.session_state.get("sim_store", {}).pop(sim_id, None)
        _save_sim_index([x for x in _load_sim_index() if x["id"] != sim_id])

    def _run_agent_bg(sim_id: str, live: bool):
        """백그라운드 스레드: 에이전트 1회 실행 후 GCS 저장"""
        from google.cloud import storage as _gcs

        def _write(d):
            try:
                _gcs.Client().bucket(_GCS_DATA_BUCKET).blob(
                    f"simulations/{sim_id}.json"
                ).upload_from_string(json.dumps(d, ensure_ascii=False), content_type="application/json")
            except Exception:
                pass

        def _mark_done_in_index(finished_at):
            try:
                idx_blob = _gcs.Client().bucket(_GCS_DATA_BUCKET).blob("simulations/index.json")
                idx = json.loads(idx_blob.download_as_text()) if idx_blob.exists() else []
                entry = next((x for x in idx if x["id"] == sim_id), None)
                if entry:
                    entry["status"] = "done"
                    entry["finished_at"] = finished_at
                idx_blob.upload_from_string(json.dumps(idx, ensure_ascii=False), content_type="application/json")
            except Exception:
                pass

        if not live:
            os.environ["DRY_RUN"] = "true"
        data = _load_sim_data(sim_id) or {
            "id": sim_id, "status": "running",
            "created_at": get_now_kst().isoformat(), "results": {},
        }
        try:
            from agent.trader import TradingAgent
            agent = TradingAgent()
            result = agent.run([], sim_datetime=None, sim_portfolio_in=None)
            data["results"]["분석"] = {"result": result, "tool_log": agent.tool_call_log}
            _write(data)
        except Exception as e:
            data["results"]["분석"] = {"result": f"❌ 실행 오류: {e}", "tool_log": []}
            _write(data)
        finally:
            os.environ["DRY_RUN"] = "false"
            data["status"] = "done"
            data["finished_at"] = get_now_kst().isoformat()
            _write(data)
            _mark_done_in_index(data["finished_at"])

    def _show_sim_results(sim_data: dict):
        status = sim_data.get("status", "")
        finished_disp = (sim_data.get("finished_at") or "")[:16].replace("T", " ")
        results = sim_data.get("results", {})
        if status == "running":
            st.warning("⏳ 실행 중... — 새로고침으로 업데이트")
        elif status == "done":
            st.success(f"✅ 완료 — {finished_disp}")
        if not results:
            return
        entry = list(results.values())[0]
        st.markdown(entry.get("result", "결과 없음"))
        tool_log = entry.get("tool_log", [])
        if tool_log:
            with st.expander(f"🔍 함수 호출 플로우 ({len(tool_log)}회)"):
                for e in tool_log:
                    args_str = ", ".join(f"{k}={v}" for k, v in e["args"].items()) if e["args"] else ""
                    st.markdown(f"**[Round {e['round']}]** `{e['tool']}({args_str})`")
                    st.code(e["result_preview"], language="json")

    # ══ 섹션 1: 결과 목록 + 인라인 상세 ════════════════════
    st.subheader("📋 실행 결과 목록")
    sim_index = _load_sim_index()
    selected_id = st.session_state.get("selected_sim_id")

    if not sim_index:
        st.info("아직 실행 기록이 없습니다.")
    else:
        for i, entry in enumerate(sim_index):
            eid = entry["id"]
            created = (entry.get("created_at") or "")[:16].replace("T", " ")
            mode = entry.get("mode", "시뮬")
            mode_badge = "🔴 실전" if mode == "실전" else "🧪 시뮬"
            status_icon = "✅" if entry.get("status") == "done" else "⏳"
            is_open = selected_id == eid
            toggle_label = "▲" if is_open else "▼"

            cols = st.columns([2.2, 0.7, 0.45, 0.45])
            cols[0].markdown(f"{status_icon} **{created}**")
            cols[1].caption(mode_badge)
            if cols[2].button(toggle_label, key=f"v_{eid}_{i}", use_container_width=True):
                if is_open:
                    st.session_state.pop("selected_sim_id", None)
                else:
                    st.session_state["selected_sim_id"] = eid
                st.rerun()
            if cols[3].button("🗑", key=f"d_{eid}_{i}", use_container_width=True):
                _delete_sim(eid)
                if is_open:
                    st.session_state.pop("selected_sim_id", None)
                st.rerun()

            if is_open:
                sim_data = _load_sim_data(eid)
                if sim_data:
                    _show_sim_results(sim_data)
                else:
                    st.warning("데이터를 찾을 수 없습니다.")
                st.divider()

    st.divider()

    # ══ 섹션 2: 실행 ══════════════════════
    st.subheader("🚀 에이전트 실행")

    if LIVE:
        st.error("🔴 **장중입니다 — 실제 계좌로 실제 주문이 실행됩니다.**")
        run_label = "📈 실행 (실제 주문)"
    else:
        st.info("🧪 장외 — 가상 ₩1,000,000으로 시뮬레이션합니다.")
        run_label = "🧪 실행 (시뮬레이션)"

    if _GCS_DATA_BUCKET:
        col_run, col_refresh, col_info = st.columns([1, 1, 2])
        run_btn = col_run.button(run_label, use_container_width=True, type="primary")
        col_refresh.button("🔄 결과 확인", use_container_width=True)
        col_info.caption("앱 전환 후 돌아와도 '결과 확인'으로 진행 상황을 볼 수 있습니다")

        if run_btn:
            _now = get_now_kst()
            sim_id = _now.strftime("%Y%m%d_%H%M%S")
            created_at = _now.isoformat()
            mode_str = "실전" if LIVE else "시뮬"
            _save_sim_data(sim_id, {
                "id": sim_id, "status": "running",
                "mode": mode_str, "created_at": created_at, "results": {},
            })
            idx = _load_sim_index()
            idx.insert(0, {
                "id": sim_id, "created_at": created_at,
                "mode": mode_str, "status": "running", "finished_at": None,
            })
            _save_sim_index(idx)
            threading.Thread(
                target=_run_agent_bg,
                args=(sim_id, LIVE),
                daemon=True,
            ).start()
            st.session_state["selected_sim_id"] = sim_id
            st.rerun()

    else:
        # 로칼 모드: 동기 실행
        run_btn = st.button(run_label, type="primary")
        if run_btn:
            _now = get_now_kst()
            sim_id = _now.strftime("%Y%m%d_%H%M%S")
            created_at = _now.isoformat()
            mode_str = "실전" if LIVE else "시뮬"
            if not LIVE:
                os.environ["DRY_RUN"] = "true"
            progress = st.progress(0, text="에이전트 실행 중...")
            try:
                from agent.trader import TradingAgent
                agent = TradingAgent()
                result_text = agent.run([], sim_datetime=None, sim_portfolio_in=None)
                tool_log = agent.tool_call_log
                progress.progress(1.0, text="✅ 완료!")
            except Exception as e:
                result_text = f"❌ 실행 오류: {e}"
                tool_log = []
            finally:
                os.environ["DRY_RUN"] = "false"
            finished_at = get_now_kst().isoformat()
            sim_data_new = {
                "id": sim_id, "status": "done", "mode": mode_str,
                "created_at": created_at, "finished_at": finished_at,
                "results": {"분석": {"result": result_text, "tool_log": tool_log}},
            }
            _save_sim_data(sim_id, sim_data_new)
            idx_list = _load_sim_index()
            idx_list.insert(0, {
                "id": sim_id, "created_at": created_at,
                "mode": mode_str, "status": "done", "finished_at": finished_at,
            })
            _save_sim_index(idx_list)
            st.session_state["selected_sim_id"] = sim_id
            st.rerun()
