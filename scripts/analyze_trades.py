"""
매매 로그 분석 스크립트
사용법: python scripts/analyze_trades.py [--file logs/trades.jsonl]

대시보드에서 다운로드한 JSONL을 logs/trades.jsonl에 저장 후 실행하면
Claude Code가 이 스크립트 결과를 읽어 분석합니다.
"""
import json
import sys
import os
from collections import defaultdict

LOG_PATH = "logs/trades.jsonl"
if len(sys.argv) > 2 and sys.argv[1] == "--file":
    LOG_PATH = sys.argv[2]

if not os.path.exists(LOG_PATH):
    print(f"파일 없음: {LOG_PATH}")
    print("대시보드 > 매매이력 탭 > 'JSONL 다운로드' 후 logs/trades.jsonl 로 저장하세요.")
    sys.exit(1)

trades = []
with open(LOG_PATH, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            trades.append(json.loads(line))

if not trades:
    print("로그 없음")
    sys.exit(0)

buys  = [t for t in trades if t["action"] == "BUY"  and t.get("success")]
sells = [t for t in trades if t["action"] == "SELL" and t.get("success")]

print("=" * 60)
print(f"총 거래 건수: {len(trades)}  (매수 {len(buys)}, 매도 {len(sells)})")
print("=" * 60)

# ── 기간 ──────────────────────────────────────────────────
if trades:
    print(f"기간: {trades[0]['ts'][:10]} ~ {trades[-1]['ts'][:10]}")
print()

# ── 실현손익 합계 ─────────────────────────────────────────
total_profit = sum(t.get("profit", 0) or 0 for t in sells)
wins  = [t for t in sells if (t.get("profit") or 0) > 0]
losses= [t for t in sells if (t.get("profit") or 0) < 0]
win_rate = len(wins) / len(sells) * 100 if sells else 0

print(f"[실현손익 합계]  ₩{total_profit:+,.0f}")
print(f"[승률]           {win_rate:.1f}%  (승 {len(wins)} / 패 {len(losses)})")
if wins:
    print(f"[평균 수익]     ₩{sum(t['profit'] for t in wins)/len(wins):+,.0f}")
if losses:
    print(f"[평균 손실]     ₩{sum(t['profit'] for t in losses)/len(losses):+,.0f}")
print()

# ── VWAP 구간별 성과 (매수 기준) ──────────────────────────
print("[VWAP 구간별 매수 성과]")
print(f"{'구간':<14} {'건수':>4} {'승률':>7} {'평균손익':>12}")
print("-" * 42)

vwap_bands = [
    ("음수 (<0%)",    None, 0),
    ("0~+1%",         0,   1),
    ("1~+2%",         1,   2),
    ("2~+3%",         2,   3),
    ("+3% 초과",      3, None),
    ("미기록",      "na", "na"),
]

# 매수-매도 페어링: ticker + 날짜로 매칭
buy_map = {}
for b in buys:
    key = (b["ticker"], b["ts"][:10])
    buy_map.setdefault(key, []).append(b)

sell_profit_by_buy = defaultdict(list)
for s in sells:
    key = (s["ticker"], s["ts"][:10])
    bs = buy_map.get(key, [])
    vd = bs[0].get("vwap_dev") if bs else None
    sell_profit_by_buy[vd].append(s.get("profit", 0) or 0)

def band_key(vd):
    if vd is None:
        return "na"
    if vd < 0:
        return "neg"
    if vd < 1:
        return "0-1"
    if vd < 2:
        return "1-2"
    if vd < 3:
        return "2-3"
    return "3+"

band_profits = defaultdict(list)
for vd, profits in sell_profit_by_buy.items():
    band_profits[band_key(vd)].extend(profits)

band_order = [
    ("neg",  "음수 (<0%)"),
    ("0-1",  "0~+1%"),
    ("1-2",  "+1~+2%"),
    ("2-3",  "+2~+3%"),
    ("3+",   "+3% 초과"),
    ("na",   "미기록"),
]
for bk, label in band_order:
    pl = band_profits.get(bk, [])
    if not pl:
        continue
    w = sum(1 for p in pl if p > 0)
    wr = w / len(pl) * 100
    avg = sum(pl) / len(pl)
    print(f"{label:<14} {len(pl):>4}건  {wr:>6.1f}%  ₩{avg:>+10,.0f}")
print()

# ── HA 패턴별 승률 (매수 기준) ────────────────────────────
print("[HA 패턴별 매수 성과]")
print(f"{'패턴':<14} {'건수':>4} {'승률':>7} {'평균손익':>12}")
print("-" * 42)

ha_profits = defaultdict(list)
for s in sells:
    key = (s["ticker"], s["ts"][:10])
    bs = buy_map.get(key, [])
    hp = bs[0].get("ha_pattern", "") if bs else ""
    ha_profits[hp or "미기록"].append(s.get("profit", 0) or 0)

for hp, pl in sorted(ha_profits.items(), key=lambda x: -len(x[1])):
    w = sum(1 for p in pl if p > 0)
    wr = w / len(pl) * 100
    avg = sum(pl) / len(pl)
    print(f"{hp:<14} {len(pl):>4}건  {wr:>6.1f}%  ₩{avg:>+10,.0f}")
print()

# ── 종목별 손익 top/bottom ─────────────────────────────────
print("[종목별 실현손익 Top 5]")
ticker_profit = defaultdict(int)
ticker_name = {}
for s in sells:
    ticker_profit[s["ticker"]] += s.get("profit", 0) or 0
    ticker_name[s["ticker"]] = s.get("name", s["ticker"])

ranked = sorted(ticker_profit.items(), key=lambda x: -x[1])
for tk, pl in ranked[:5]:
    print(f"  {ticker_name[tk]}({tk})  ₩{pl:+,.0f}")

if len(ranked) > 5:
    print("\n[종목별 실현손익 Bottom 5]")
    for tk, pl in ranked[-5:]:
        print(f"  {ticker_name[tk]}({tk})  ₩{pl:+,.0f}")

print()
print("=" * 60)
print("분석 완료. 이 출력을 Claude Code에 붙여넣거나 파일로 공유하세요.")
