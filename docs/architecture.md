# gayastock 아키텍처 & 동작 플로우

> 최종 업데이트: 2026-05-23 · 전략 v5 기준

---

## 1. 시스템 전체 구조

```mermaid
graph TB
    subgraph GCP["Google Cloud Platform"]
        subgraph Scheduler["Cloud Scheduler (평일 09:20~11:30, 20회/일, 7분 간격)"]
            CRON["run01~run20\n cron jobs"]
        end

        subgraph Job["Cloud Run Job — gayastock-agent"]
            MAIN["main.py --once"]
        end

        subgraph Svc["Cloud Run Service — gayastock-dashboard"]
            DASH["dashboard/app.py\n(Streamlit :8501)"]
        end

        subgraph GCS["Cloud Storage (GCS_DATA_BUCKET)"]
            TK["kis_token_cache.json"]
            TR["logs/trades.jsonl"]
            AR["logs/agent_runs.jsonl"]
            SP["session_progress.json"]
            ST["settings.json"]
            SM["simulations/*.json"]
        end

        VERTEX["Vertex AI\nGemini 2.5 Flash"]
    end

    subgraph KIS["한국투자증권 OpenAPI"]
        KISAPI["REST API\n(모의 / 실투자)"]
    end

    CRON -->|"HTTP 트리거"| Job
    MAIN -->|"function calling"| VERTEX
    MAIN -->|"주문 / 시세"| KISAPI
    MAIN -->|"read/write"| GCS
    DASH -->|"read"| GCS
    DASH -->|"잔고 / 시세"| KISAPI
    DASH -->|"수동 실행 시"| VERTEX

    Browser["🌐 브라우저"] -->|"HTTPS"| DASH
```

---

## 2. 트레이딩 루프 플로우 (Cloud Run Job)

```mermaid
flowchart TD
    START([Cloud Scheduler 트리거]) --> MAIN["main.py run_trading()"]
    MAIN --> HOLIDAY{휴장일?\n주말/공휴일}
    HOLIDAY -->|"Yes\n(--force 없으면)"| SKIP([종료])
    HOLIDAY -->|No| PRE["_check_needs_action()\nPython 사전 체크"]

    PRE --> CHK{TP/SL 조건?\n슬롯 여유?\nVWAP 이탈?}
    CHK -->|"필요 없음"| NOLOG["에이전트 스킵\n로그만 기록"]
    CHK -->|"필요"| AGENT

    subgraph AGENT["TradingAgent.run() — Gemini Function Calling"]
        direction TB
        MSG["시스템 프롬프트 + 회차 컨텍스트\n전송 (1회차: Google Search 포함)"]
        MSG --> LOOP["도구 호출 루프\n(최대 50라운드)"]

        LOOP --> FC{Gemini가\n함수 호출?}
        FC -->|Yes| EXEC["execute_tool()"]
        EXEC --> TOOLS

        subgraph TOOLS["사용 가능한 도구 (8개)"]
            direction LR
            T1["get_portfolio\n잔고·보유종목"]
            T2["get_top_volume_stocks\n거래량 Top30"]
            T3["get_heikin_ashi_candles\nVWAP + 3분봉 HA"]
            T4["get_stock_price\n현재가·지표"]
            T5["get_financial_summary\n재무 4개년"]
            T6["get_daily_price_chart\n일봉 20일"]
            T7["buy_stock\n시장가 매수"]
            T8["sell_stock\n시장가 매도"]
        end

        EXEC --> RET["결과를 Gemini에 반환"]
        RET --> LOOP
        FC -->|"No (텍스트 응답)"| FINAL["최종 보고서 텍스트"]
    end

    AGENT --> LOG["log_agent_run()\n→ GCS agent_runs.jsonl"]
    NOLOG --> END([종료])
    LOG --> END
```

---

## 3. LLM 진입 판단 플로우 (전략 v5)

```mermaid
flowchart TD
    SCAN["get_top_volume_stocks(n=30)\nETF/스팩 제외 후 상위 10종목"] --> VWAP

    subgraph VWAP["종목별 VWAP 체크 (get_heikin_ashi_candles)"]
        direction LR
        V1{"VWAP\n음수?"}
        V2{"VWAP\n+3% 초과?"}
        V3{"당일\n손절 종목?"}
    end

    VWAP --> GATE{하드 가드레일\n통과?}
    V1 -->|Yes| OUT1["❌ 진입 금지\n관성 역행"]
    V2 -->|Yes| Out2["❌ 진입 금지\n고점 추격"]
    V3 -->|Yes| Out3["❌ 진입 금지\n재진입 금지"]

    GATE -->|"통과 (VWAP 0~+3%)"| LLM

    subgraph LLM["LLM 자율 판단 (최종 후보 2~3종목)"]
        direction TB
        INFO["기본 정보\n· VWAP 이탈률\n· 거래량 순위\n· 가격 추이"]
        OPT["선택적 심층 조회\n· get_financial_summary\n· get_daily_price_chart\n· get_stock_price"]
        JUDGE["종합 판단\n· 확신도에 따라 30~50%\n· 슬롯 수 분산"]
    end

    LLM --> ORDER

    subgraph ORDER["주문 실행"]
        BUY["buy_stock()\n시장가 매수"]
        SELL["sell_stock()\nTP / SL / VWAP음수 / 강제청산"]
    end

    ORDER --> TRADELOG["log_trade()\n→ GCS trades.jsonl"]
```

---

## 4. 회차별 행동 규칙

```mermaid
gantt
    title 당일 트레이딩 타임라인 (KST)
    dateFormat HH:mm
    axisFormat %H:%M

    section 진입
    1회차 — Google 검색 + 진입     : 09:20, 7m

    section 오전 점검 (2~8회차)
    2회차 — TP/SL + 추가 진입      : 09:27, 7m
    3회차 — TP/SL + 추가 진입      : 09:34, 7m
    4회차 — TP/SL + 추가 진입      : 09:41, 7m
    5회차 — TP/SL + 추가 진입      : 09:48, 7m
    6회차 — TP/SL + 추가 진입      : 09:55, 7m
    7회차 — TP/SL + 추가 진입      : 10:02, 7m
    8회차 — TP/SL + 추가 진입      : 10:09, 7m

    section 오전 후반 (9~15회차)
    9회차 — 기준 강화              : 10:16, 7m
    10회차 — 기준 강화             : 10:23, 7m
    11회차 — 기준 강화             : 10:30, 7m
    12회차 — 기준 강화             : 10:37, 7m
    13회차 — 기준 강화             : 10:44, 7m
    14회차 — 기준 강화             : 10:51, 7m
    15회차 — 기준 강화             : 10:58, 7m

    section 정리 (16~18회차)
    16회차 — 신규 진입 원칙 금지    : 11:05, 7m
    17회차 — 신규 진입 원칙 금지    : 11:12, 7m
    18회차 — 신규 진입 원칙 금지    : 11:19, 7m

    section 청산
    19회차 — 청산 준비             : 11:26, 4m
    20회차 — 강제 전량 청산        : 11:30, 1m
```

---

## 5. 대시보드 실행 플로우

```mermaid
sequenceDiagram
    participant B as 브라우저
    participant D as dashboard/app.py
    participant P as session_progress.json<br/>(GCS / 로컬)
    participant T as 백그라운드 스레드
    participant A as TradingAgent
    participant K as KIS API

    B->>D: 비밀번호 입력 + 실행 버튼 클릭
    D->>P: _write_session_progress({status: "running"})<br/>← 스레드 시작 전에 먼저 기록
    D->>T: threading.Thread(_run_agent_bg).start()
    D->>B: st.rerun()

    loop 3초마다 자동 새로고침 (_prog_active=True)
        B->>D: 페이지 재렌더링
        D->>P: _load_session_progress()
        P-->>D: {status:"running", tool_log: [...]}
        D->>B: 실시간 진행상황 표시
    end

    T->>K: get_portfolio / get_top_volume_stocks
    T->>A: TradingAgent.run(on_tool_call=콜백)
    loop 도구 호출마다
        A->>K: 도구 실행
        A->>T: on_tool_call(tool_log)
        T->>P: _write_session_progress(tool_log 포함)
    end
    A-->>T: 최종 보고서
    T->>P: _write_session_progress({status: "done"})
    B->>D: 마지막 새로고침 → 완료 상태 표시
```

---

## 6. GCS 데이터 구조

```mermaid
graph LR
    subgraph GCS["GCS_DATA_BUCKET"]
        direction TB

        subgraph TOKEN["인증"]
            TK["kis_token_cache.json\n{access_token, expires_at}"]
        end

        subgraph LOGS["로그 (JSONL)"]
            TR["logs/trades.jsonl\n최대 1,000건\n{ts, action, ticker, qty, price,\n reason, profit, vwap_dev}"]
            AR["logs/agent_runs.jsonl\n최대 500건\n{ts, summary, portfolio,\n buy_tickers, loops}"]
        end

        subgraph DASHBOARD["대시보드"]
            SP["session_progress.json\n{status, current_loop,\n tool_log, loops}"]
            ST["settings.json\n{initial_capital}"]
            IDX["simulations/index.json"]
            SIM["simulations/{sim_id}.json\n{loops, final_summary}"]
        end
    end

    JOB["Cloud Run Job"] -->|"read/write"| TOKEN
    JOB -->|"append"| LOGS
    DASH["Dashboard"] -->|"read"| LOGS
    DASH -->|"read/write"| DASHBOARD
    DASH -->|"write"| TOKEN
```

---

## 7. KIS API 호출 맵

```mermaid
graph LR
    subgraph BROKER["broker/kis.py — KISBroker"]
        direction TB

        subgraph AUTH["인증"]
            TOK["_get_token()\nPOST /oauth2/tokenP"]
        end

        subgraph PRICE["시세"]
            VOL["get_top_volume_stocks()\nFHPST01710000"]
            CUR["get_current_price()\nFHKST01010100"]
            MIN["get_minute_candles()\nFHKST03010200\n→ 1분봉 → 3분봉 → HA + VWAP"]
            DAY["get_daily_candles()\nFHKST03010100\n→ 일봉 OHLCV"]
        end

        subgraph FINANCE["재무"]
            INC["get_income_statement()\nFHKST66430200"]
            BAL["get_balance_sheet()\nFHKST66430100"]
            RAT["get_financial_ratio()\nFHKST66430300"]
            FIN["get_financial_summary()\n↑ 세 개 통합 호출"]
        end

        subgraph ACCOUNT["계좌"]
            BAL2["get_balance()\nVTTC8434R / TTTC8434R"]
            BUY["buy_order()\nVTTC0802U / TTTC0802U"]
            SELL["sell_order()\nVTTC0801U / TTTC0801U"]
            PEND["get_pending_orders()\nVTTC8036R / TTTC8036R"]
            HIST["get_order_history()\nVTTC8001R / TTTC8001R"]
        end
    end
```

---

## 8. 모듈 의존 관계

```mermaid
graph TD
    MAIN["main.py"] --> TRADER["agent/trader.py\nTradingAgent"]
    MAIN --> BROKER["broker/kis.py\nKISBroker"]
    MAIN --> LOG["data/trade_log.py"]
    MAIN --> CFG["config.py"]

    TRADER --> TOOLS["agent/tools.py\nexecute_tool()"]
    TRADER --> LOG
    TRADER --> CFG

    TOOLS --> BROKER
    TOOLS --> LOG
    TOOLS --> FIN["data/financial.py\n_get_broker() 싱글턴"]

    FIN --> BROKER

    DASH["dashboard/app.py"] --> BROKER
    DASH --> LOG
    DASH --> TRADER
    DASH --> FIN
    DASH --> CFG

    BROKER --> UTILS["data/utils.py\nget_now_kst()"]
    LOG --> UTILS
    TRADER --> CFG
```

---

## 9. 환경변수 & 설정

| 변수 | 필수 | 기본값 | 설명 |
|------|:----:|--------|------|
| `GCP_PROJECT_ID` | ✅ | — | Vertex AI 프로젝트 ID |
| `GCP_REGION` | | `asia-northeast3` | Vertex AI 리전 |
| `GEMINI_MODEL` | | `gemini-2.5-flash` | LLM 모델 |
| `KIS_APP_KEY` | ✅ | — | KIS API 키 (Secret Manager) |
| `KIS_APP_SECRET` | ✅ | — | KIS API 시크릿 (Secret Manager) |
| `KIS_ACCOUNT_NO` | ✅ | — | 계좌번호 `XXXXXXXX-XX` |
| `KIS_MOCK` | | `true` | `true`=모의투자 / `false`=실계좌 |
| `DRY_RUN` | | `false` | `true`=주문 전송 안 함 |
| `GCS_DATA_BUCKET` | | — | 로그·토큰 저장 버킷 |
| `GCS_TOKEN_BUCKET` | | — | 토큰 전용 버킷 (없으면 DATA_BUCKET 공유) |
| `LOG_DIR` | | `logs` | 로컬 로그 디렉토리 |
| `MAX_POSITIONS` | | `5` | 최대 보유 종목 수 |
| `TAKE_PROFIT_PCT` | | `4.0` | 익절 기준 (%) |
| `STOP_LOSS_PCT` | | `2.5` | 손절 기준 (%) |
| `INNER_LOOP_COUNT` | | `1` | 루프 반복 횟수 |
| `INNER_LOOP_SLEEP_SEC` | | `90` | 루프 간 대기 (초) |
| `INITIAL_CAPITAL` | | `0` | 투자 원금 (대시보드 표시) |

---

## 10. 배포 파이프라인

```mermaid
flowchart LR
    GH["GitHub\nmain 브랜치 push"] -->|"trigger"| GA

    subgraph GA["GitHub Actions — deploy.yml"]
        direction TB
        AUTH2["GCP 인증\n(Workload Identity)"]
        BUCKET["GCS 버킷 생성\n(없으면 자동 생성)"]
        BUILD["Docker 이미지 빌드 & 푸시\n· app (대시보드)\n· agent (트레이딩)"]
        DEPLOY_SVC["Cloud Run Service 배포\ngayastock-dashboard"]
        DEPLOY_JOB["Cloud Run Job 배포\ngayastock-agent"]
        SCHED["Cloud Scheduler 업데이트\nrun01~run20 (20개)"]

        AUTH2 --> BUCKET --> BUILD --> DEPLOY_SVC --> DEPLOY_JOB --> SCHED
    end
```

---

## 11. 주요 설계 결정

| 항목 | 결정 | 이유 |
|------|------|------|
| LLM 진입 조건 | VWAP 가드레일 + LLM 자율 판단 | 기계적 HA 규칙 제거, 시장 맥락 반영 |
| HA 패턴 | 참고 데이터로만 제공 (진입 조건 아님) | 오신호 빈발, LLM이 더 유연하게 판단 |
| 포지션 사이징 | 가용예수금 최대 50% | 연속 손실 방지 |
| 당일 손절 재진입 | 금지 | 쏠리드/한국첨단소재 사례 재발 방지 |
| 강제 청산 | 11:30 전 전량 매도 | 당일 리스크 제거 |
| 토큰 캐시 | GCS 우선 → 로컬 fallback | Cloud Run 재시작마다 재발급 방지 |
| GCS 쓰기 | generation match + 재시도 5회 | 동시 실행 시 로그 유실 방지 |
| 진행상황 기록 | 스레드 시작 전 먼저 기록 | st.rerun() 레이스 컨디션 방지 |
| 심층 분석 도구 | 최종 후보 2~3종목에만 사용 | MAX_TOOL_ROUNDS=50 내 매매 라운드 확보 |
