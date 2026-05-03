"""gayastock 대시보드 — streamlit run dashboard/app.py"""
import sys
import os
import json
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
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

# ── 사이드바 ──────────────────────────────────────────────
st.sidebar.title("📈 gayastock")
st.sidebar.caption("AI 주식 트레이딩 에이전트")
page = st.sidebar.radio("메뉴", ["포트폴리오", "매매 이력", "에이전트 로그", "Dry Run 시뮬레이션"])
refresh = st.sidebar.button("🔄 새로고침")

@st.cache_data(ttl=30)
def load_balance():
    try:
        broker = KISBroker()
        return broker.get_balance()
    except Exception as e:
        return {"error": str(e)}

if refresh:
    st.cache_data.clear()

# ══════════════════════════════════════════════════════════
if page == "포트폴리오":
    st.title("포트폴리오 현황")

    data = load_balance()

    if "error" in data:
        st.error("잔고 조회에 실패했습니다. API 설정을 확인하세요.")
        st.stop()

    # 상단 요약 카드
    col1, col2, col3, col4 = st.columns(4)
    cash = data.get("cash", 0)
    total_eval = data.get("total_eval", 0)
    profit_loss = data.get("profit_loss", 0)
    holdings = data.get("holdings", [])

    holding_eval = sum(h["current_price"] * h["quantity"] for h in holdings)
    total_invested = total_eval - cash
    pl_rate = (profit_loss / (total_invested - profit_loss) * 100) if (total_invested - profit_loss) > 0 else 0

    col1.metric("예수금", f"₩{cash:,.0f}")
    col2.metric("평가금액", f"₩{total_eval:,.0f}")
    col3.metric("평가손익", f"₩{profit_loss:,.0f}", f"{pl_rate:+.2f}%",
                delta_color="normal" if profit_loss >= 0 else "inverse")
    col4.metric("보유 종목 수", f"{len(holdings)}개")

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
            df[display_cols].style.applymap(color_pl, subset=["수익률(%)"]).format({
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
            values.append(cash)
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

# ══════════════════════════════════════════════════════════
elif page == "매매 이력":
    st.title("매매 이력")

    tab_kis, tab_agent = st.tabs(["📋 KIS 계좌 체결 이력", "🤖 에이전트 주문 로그"])

    # ── KIS 실계좌 체결 이력 ──────────────────────────────
    with tab_kis:
        col_date1, col_date2, col_btn = st.columns([2, 2, 1])
        with col_date1:
            start_d = st.date_input("시작일", value=datetime.now().date() - timedelta(days=30))
        with col_date2:
            end_d = st.date_input("종료일", value=datetime.now().date())
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

    # ── 에이전트 주문 로그 ────────────────────────────────
    with tab_agent:
        trades = get_trades()
        if not trades:
            st.info("아직 에이전트 주문 기록이 없습니다. 에이전트를 실행하면 여기에 기록됩니다.")
        else:
            df = pd.DataFrame(trades)
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.sort_values("ts", ascending=False)

            col1, col2, col3 = st.columns(3)
            col1.metric("총 주문", len(df))
            col2.metric("매수", len(df[df["action"] == "BUY"]))
            col3.metric("매도", len(df[df["action"] == "SELL"]))

            st.divider()

            display_df = df[["ts", "action", "ticker", "quantity", "price", "amount", "success", "reason"]].copy()
            display_df.columns = ["일시", "구분", "종목코드", "수량", "체결가", "금액", "성공", "판단 근거"]
            display_df["일시"] = display_df["일시"].dt.strftime("%Y-%m-%d %H:%M")
            display_df["체결가"] = display_df["체결가"].apply(lambda x: f"₩{x:,.0f}")
            display_df["금액"] = display_df["금액"].apply(lambda x: f"₩{x:,.0f}")
            display_df["구분"] = display_df["구분"].apply(lambda x: "🟢 매수" if x == "BUY" else "🔴 매도")
            st.dataframe(display_df, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════
elif page == "에이전트 로그":
    st.title("에이전트 실행 로그")

    runs = get_agent_runs()
    if not runs:
        st.info("아직 에이전트 실행 기록이 없습니다. `python main.py --once` 로 실행해보세요.")
        st.stop()

    runs_reversed = list(reversed(runs))

    for i, run in enumerate(runs_reversed[:20]):
        ts = run.get("ts", "")
        watchlist = run.get("watchlist", [])
        summary = run.get("summary", "")
        portfolio = run.get("portfolio", {})

        cash = portfolio.get("cash", 0)
        total_eval = portfolio.get("total_eval", 0)
        holdings_count = len(portfolio.get("holdings", []))

        label = f"🤖 {ts[:16]}  |  분석종목: {', '.join(watchlist)}  |  잔고: ₩{total_eval:,.0f}"
        with st.expander(label, expanded=(i == 0)):
            col1, col2, col3 = st.columns(3)
            col1.metric("예수금", f"₩{cash:,.0f}")
            col2.metric("총 평가금액", f"₩{total_eval:,.0f}")
            col3.metric("보유 종목", f"{holdings_count}개")

            st.markdown("**에이전트 판단 요약**")
            st.markdown(summary)

# ══════════════════════════════════════════════════════════
elif page == "Dry Run 시뮬레이션":
    st.title("🧪 Dry Run 시뮬레이션")
    st.caption("실제 주문 없이 스케줄러 3회 실행(09:10 / 12:00 / 14:30) 시 에이전트 판단을 시뮬레이션합니다.")

    # ── 비밀번호 확인 ──────────────────────────────────────
    pw = st.text_input("비밀번호", type="password", placeholder="비밀번호를 입력하세요")
    if pw and pw != "1018":
        st.error("비밀번호가 올바르지 않습니다.")
        st.stop()
    if not pw:
        st.stop()

    # ── GCS 기반 결과 저장/로드 ────────────────────────────
    _GCS_DATA_BUCKET = os.environ.get("GCS_DATA_BUCKET", "")
    _SIM_BLOB = "simulations/dry_run_latest.json"

    def _save_sim_to_gcs(data: dict):
        if not _GCS_DATA_BUCKET:
            return
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(_SIM_BLOB)
            blob.upload_from_string(json.dumps(data, ensure_ascii=False), content_type="application/json")
        except Exception:
            pass

    def _load_sim_from_gcs() -> dict:
        if not _GCS_DATA_BUCKET:
            return {}
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(_GCS_DATA_BUCKET).blob(_SIM_BLOB)
            if blob.exists():
                return json.loads(blob.download_as_text())
        except Exception:
            pass
        return {}

    def _run_simulation_bg(schedule_times, watchlist, base_date_str):
        """백그라운드 스레드에서 시뮬레이션 실행 후 GCS에 저장"""
        os.environ["DRY_RUN"] = "true"
        data = {
            "status": "running",
            "base_date": base_date_str,
            "started_at": datetime.now().isoformat(),
            "results": {},
        }
        _save_sim_to_gcs(data)
        try:
            from agent.trader import TradingAgent
            for sim_dt, label in schedule_times:
                try:
                    agent = TradingAgent()
                    result = agent.run(watchlist, sim_datetime=sim_dt)
                    tool_log = agent.tool_call_log
                except Exception as e:
                    result = f"❌ 실행 오류: {e}"
                    tool_log = []
                data["results"][label] = {"result": result, "tool_log": tool_log}
                data["status"] = "running"
                _save_sim_to_gcs(data)
        finally:
            os.environ["DRY_RUN"] = "false"
            data["status"] = "done"
            data["finished_at"] = datetime.now().isoformat()
            _save_sim_to_gcs(data)

    # ── 기준 날짜 선택 (거래일만 표시) ────────────────────
    import holidays as hol

    def get_recent_trading_days(n=60):
        now = datetime.now()
        kr_holidays = hol.Korea(years=[now.year, now.year - 1])
        today = now.date()
        market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        result = []
        check = today
        if today.weekday() >= 5 or today in kr_holidays or now < market_open:
            check -= timedelta(days=1)
        while len(result) < n:
            if check.weekday() < 5 and check not in kr_holidays:
                result.append(check)
            check -= timedelta(days=1)
        return result

    trading_days = get_recent_trading_days(60)
    day_labels = [f"{d.strftime('%Y-%m-%d')} ({['월','화','수','목','금'][d.weekday()]})" for d in trading_days]
    selected_label = st.selectbox("시뮬레이션 날짜 (거래일만 표시)", day_labels, index=0)
    base_date = trading_days[day_labels.index(selected_label)]
    schedule_times = [
        (datetime.combine(base_date, datetime.strptime("09:10", "%H:%M").time()), "09:10 오전"),
        (datetime.combine(base_date, datetime.strptime("12:00", "%H:%M").time()), "12:00 점심"),
        (datetime.combine(base_date, datetime.strptime("14:30", "%H:%M").time()), "14:30 오후"),
    ]

    DEFAULT_WATCHLIST = [
        "005930", "000660", "066570",
        "035420", "035720",
        "005380", "000270", "012330",
        "373220", "006400", "051910",
        "005490", "010130",
        "105560", "055550", "086790",
        "207940", "068270",
        "017670", "028260",
    ]

    st.info(f"📅 기준일 **{base_date.strftime('%Y-%m-%d')}** | 현재 포트폴리오 + 현재 가격 기준 — 실제 주문 없음")
    st.divider()

    # ── 실행 버튼 + 결과 확인 버튼 ────────────────────────
    col_run, col_refresh, col_info = st.columns([1, 1, 2])
    with col_run:
        run_btn = st.button("🚀 시뮬레이션 실행", use_container_width=True, type="primary")
    with col_refresh:
        refresh_btn = st.button("🔄 결과 확인", use_container_width=True)
    with col_info:
        st.caption("앱 전환 후 돌아와도 '결과 확인'으로 진행 상황을 볼 수 있습니다")

    # ── GCS 없는 환경 (로컬) 폴백: 세션 내 동기 실행 ──────
    if not _GCS_DATA_BUCKET:
        if run_btn:
            os.environ["DRY_RUN"] = "true"
            try:
                from agent.trader import TradingAgent
                tabs = st.tabs([f"⏰ {label}" for _, label in schedule_times])
                for (sim_dt, label), tab in zip(schedule_times, tabs):
                    with tab:
                        with st.spinner(f"{label} 판단 중..."):
                            try:
                                agent = TradingAgent()
                                result = agent.run(DEFAULT_WATCHLIST, sim_datetime=sim_dt)
                                tool_log = agent.tool_call_log
                            except Exception as e:
                                result = f"❌ 실행 오류: {e}"
                                tool_log = []
                        st.success(f"{label} 완료")
                        st.markdown(result)
                        if tool_log:
                            with st.expander(f"🔍 함수 호출 플로우 ({len(tool_log)}회)", expanded=False):
                                for entry in tool_log:
                                    args_str = ", ".join(f"{k}={v}" for k, v in entry["args"].items()) if entry["args"] else ""
                                    st.markdown(f"**[Round {entry['round']}]** `{entry['tool']}({args_str})`")
                                    st.code(entry["result_preview"], language="json")
            finally:
                os.environ["DRY_RUN"] = "false"
        st.stop()

    # ── GCS 환경: 백그라운드 실행 ─────────────────────────
    if run_btn:
        t = threading.Thread(
            target=_run_simulation_bg,
            args=(schedule_times, DEFAULT_WATCHLIST, base_date.strftime("%Y-%m-%d")),
            daemon=True,
        )
        t.start()
        st.success("✅ 시뮬레이션이 백그라운드에서 시작되었습니다. 다른 앱을 사용하다 돌아와서 '결과 확인'을 누르세요.")

    # ── 결과 표시 ─────────────────────────────────────────
    sim_data = _load_sim_from_gcs()
    if not sim_data:
        st.info("아직 실행된 시뮬레이션 결과가 없습니다.")
        st.stop()

    status = sim_data.get("status", "")
    sim_base = sim_data.get("base_date", "")
    started_at = sim_data.get("started_at", "")[:16].replace("T", " ")
    finished_at = sim_data.get("finished_at", "")
    results = sim_data.get("results", {})

    if status == "running":
        completed = len(results)
        st.warning(f"⏳ 실행 중... ({completed}/3 완료) — 시작: {started_at}")
        if results:
            st.caption("완료된 결과 미리보기:")
    elif status == "done":
        finished_str = finished_at[:16].replace("T", " ") if finished_at else ""
        st.success(f"✅ 완료 — 기준일: {sim_base} | 종료: {finished_str}")

    if results:
        tabs = st.tabs([f"⏰ {label}" for label in results])
        for tab, (label, entry) in zip(tabs, results.items()):
            with tab:
                st.markdown(entry.get("result", "결과 없음"))
                tool_log = entry.get("tool_log", [])
                if tool_log:
                    st.divider()
                    with st.expander(f"🔍 함수 호출 플로우 ({len(tool_log)}회)", expanded=False):
                        for e in tool_log:
                            args_str = ", ".join(f"{k}={v}" for k, v in e["args"].items()) if e["args"] else ""
                            st.markdown(f"**[Round {e['round']}]** `{e['tool']}({args_str})`")
                            st.code(e["result_preview"], language="json")
