"""gayastock 대시보드 — streamlit run dashboard/app.py"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
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
page = st.sidebar.radio("메뉴", ["포트폴리오", "매매 이력", "예수금 입출금", "에이전트 로그"])
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
elif page == "예수금 입출금":
    st.title("예수금 입출금 내역")

    col_date1, col_date2, col_btn = st.columns([2, 2, 1])
    with col_date1:
        dep_start = st.date_input("시작일", value=datetime.now().date() - timedelta(days=30), key="dep_start")
    with col_date2:
        dep_end = st.date_input("종료일", value=datetime.now().date(), key="dep_end")
    with col_btn:
        st.write("")
        dep_btn = st.button("조회", use_container_width=True, key="dep_btn")

    @st.cache_data(ttl=60)
    def load_deposit_history(start: str, end: str):
        try:
            broker = KISBroker()
            return broker.get_deposit_history(start, end)
        except Exception as e:
            return {"error": str(e)}

    if dep_btn or "dep_history" not in st.session_state:
        st.session_state["dep_history"] = load_deposit_history(
            dep_start.strftime("%Y%m%d"), dep_end.strftime("%Y%m%d")
        )

    dep_data = st.session_state.get("dep_history", [])

    if isinstance(dep_data, dict) and "error" in dep_data:
        st.error("예수금 입출금 내역 조회에 실패했습니다.")
        with st.expander("오류 상세 (디버그)"):
            st.code(dep_data["error"])
        st.stop()

    if not dep_data:
        st.info("해당 기간에 입출금 내역이 없습니다.")
        st.stop()

    ddf = pd.DataFrame(dep_data)
    ddf["date"] = pd.to_datetime(ddf["date"], format="%Y%m%d", errors="coerce")

    total_in = ddf[ddf["type"] == "입금"]["amount"].sum()
    total_out = ddf[ddf["type"] == "출금"]["amount"].sum()

    col1, col2, col3 = st.columns(3)
    col1.metric("총 입금", f"₩{total_in:,.0f}")
    col2.metric("총 출금", f"₩{total_out:,.0f}")
    col3.metric("순 입출금", f"₩{total_in - total_out:,.0f}")

    st.divider()

    disp = ddf[["date", "type", "amount", "description", "balance"]].copy()
    disp.columns = ["날짜", "구분", "금액", "거래내용", "잔액"]
    disp["날짜"] = disp["날짜"].dt.strftime("%Y-%m-%d")
    disp["금액"] = disp["금액"].apply(lambda x: f"₩{x:,.0f}")
    disp["잔액"] = disp["잔액"].apply(lambda x: f"₩{x:,.0f}" if x else "-")
    disp["구분"] = disp["구분"].apply(lambda x: "🔵 입금" if x == "입금" else "🟠 출금")
    st.dataframe(disp, use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("일별 입출금 추이")
    ddf["date_only"] = ddf["date"].dt.date
    daily_dep = ddf.groupby(["date_only", "type"])["amount"].sum().reset_index()
    fig = px.bar(daily_dep, x="date_only", y="amount", color="type",
                 color_discrete_map={"입금": "#3498db", "출금": "#e67e22"},
                 labels={"date_only": "날짜", "amount": "금액 (원)", "type": "구분"})
    fig.update_layout(height=300, margin=dict(t=20, b=20, l=20, r=20))
    st.plotly_chart(fig, use_container_width=True)

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
