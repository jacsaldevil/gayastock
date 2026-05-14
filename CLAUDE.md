# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

gayastock은 Google Vertex AI(Gemini)를 사용하는 한국 주식 자동매매 에이전트입니다. 한국투자증권(KIS) OpenAPI를 통해 주문을 실행하며, Cloud Run에 배포됩니다.

## 실행 명령어

```bash
# 로컬 인증 (최초 1회)
gcloud auth application-default login

# 패키지 설치
pip install -r requirements.txt

# 에이전트 1회 실행
python main.py --once

# 시뮬레이션 모드 (실제 주문 없음)
python main.py --once --dry-run

# 특정 종목만 분석
python main.py --once --tickers 005930 000660

# 대시보드 실행
streamlit run dashboard/app.py
```

## 환경 변수 (.env)

`.env.example` 참조. 핵심 변수:
- `KIS_MOCK=true` → 모의투자 / `false` → 실계좌
- `DRY_RUN=true` → KIS API는 호출하되 실제 주문은 전송하지 않음
- `GCS_DATA_BUCKET` → 설정 시 로그를 GCS에 저장, 없으면 로컬 `logs/` 폴더

## 아키텍처

### 데이터 흐름
```
Cloud Scheduler (20회/일, KST 09:20~15:10, 17분 간격)
    → Cloud Run Job (main.py --once)
        → TradingAgent.run()
            → Gemini API (function calling 루프, 최대 30라운드)
                → execute_tool() → KISBroker
            → log_agent_run() → GCS or 로컬 JSONL
```

### 핵심 모듈

**`agent/trader.py` — TradingAgent**
- Gemini function calling 루프를 관리하는 핵심 클래스
- 1회차(09:20)에만 Google Search Grounding 사용, 이후는 `_model_no_search`
- `_SCHEDULE_SLOTS`로 현재 회차를 판별해 시스템 프롬프트 컨텍스트 제공
- `MAX_TOOL_ROUNDS = 30`

**`agent/tools.py` — 도구 정의 및 실행**
- Gemini에 노출되는 5개 tool: `get_portfolio`, `get_stock_price`, `get_top_volume_stocks`, `get_heikin_ashi_candles`, `buy_stock`, `sell_stock`
- `DRY_RUN=true` 시 `_sim_portfolio` 딕셔너리로 가상 포트폴리오 시뮬레이션
- `_broker()`는 `data/financial.py`의 싱글턴 KISBroker 인스턴스를 공유

**`broker/kis.py` — KISBroker**
- KIS OpenAPI 전체 래퍼 (토큰 발급/갱신, 현재가, 잔고, 주문, 체결이력 등)
- `get_minute_candles()`: 3분봉 → 하이킨아시 계산 후 반환
  - 강한상승: `upper_wick < |body| × 0.15` (양봉)
  - 강한하락: `lower_wick < |body| × 0.15` (음봉)
- `get_balance()`: `dnca_tot_amt`(원금)와 `tot_evlu_amt`(총평가)를 분리해서 반환

**`prompts/system_prompt.md`**
- Gemini에 주입되는 트레이딩 전략 전문. `{MAX_POSITIONS}`, `{TAKE_PROFIT_PCT}`, `{STOP_LOSS_PCT}` 플레이스홀더 포함
- 회차별 행동 규칙(1회차 진입 / 2~6회차 오전 / 7~11회차 오전후반 / 12~17회차 오후 / 18~19회차 후반 / 20회차 강제청산) 정의

**`data/trade_log.py` — 로그 저장소**
- `log_trade()` / `log_agent_run()`: GCS 또는 로컬 JSONL 파일에 저장
- 보존 한도: 매매 로그 1,000건, 에이전트 실행 로그 500건 (자동 trim)
- GCS append는 read-modify-write 패턴 (원자적이지 않음)

**`dashboard/app.py` — Streamlit 대시보드**
- 포트폴리오 현황 / 매매이력 / 에이전트 로그 / 에이전트 실행 4개 페이지
- 포트폴리오 카드: 투자금액(`cash`=dnca_tot_amt) / 예수금(`total_eval - holding_eval`) / 평가금액 / 평가손익 / 보유종목수
- 손익률 추이 차트: `get_agent_runs()` 스냅샷 기준, 일별 마지막 값

## 배포 구조

- **Cloud Run Service**: Streamlit 대시보드 (`dashboard/app.py`, `Dockerfile`)
- **Cloud Run Job**: 트레이딩 에이전트 (`main.py --once`, `Dockerfile.agent`)
- **Cloud Scheduler**: 20개 job (run01~run20), `Asia/Seoul` 타임존 KST 직접 사용
- **GitHub Actions** (`.github/workflows/deploy.yml`): 이미지 빌드 → Cloud Run 배포 → Scheduler 업데이트

## 주의사항

- 스케줄 시간 변경 시 `agent/trader.py`의 `_SCHEDULE_SLOTS`와 `deploy.yml`의 cron, `prompts/system_prompt.md` 세 곳을 함께 수정해야 함
- KIS 토큰은 만료 시 자동 재발급됨 (`broker/kis.py` 내부 처리)
- `KIS_MOCK=true` 상태에서도 `DRY_RUN=false`면 모의투자 계좌에 실제 주문이 들어감
- Google Search Grounding: SDK 버전에 따라 `grounding.GoogleSearch()` → `Tool(google_search={})` → 검색 없이 실행 순으로 폴백
