# ADR-005: JSONL 파일 기반 로그 저장

- **상태**: Accepted
- **날짜**: 2026-05-01

## 배경

에이전트의 매매 이력과 실행 요약을 대시보드에서 조회하려면 영속 저장소가 필요하다. 초기 단계에서 적절한 저장소 방식을 선택해야 했다.

## 결정

**매매 이력과 에이전트 실행 로그를 JSONL(JSON Lines) 파일로 저장한다.**

두 파일로 분리:
- `logs/trades.jsonl`: 매수/매도 실행 건별 기록 (action, ticker, quantity, price, reason, success)
- `logs/agent_runs.jsonl`: 에이전트 실행 회차별 요약 (watchlist, summary, portfolio snapshot)

`LOG_DIR` 환경변수로 경로를 분리해 로컬/클라우드 환경에 동일한 코드로 대응한다.

## 검토한 대안

| 방식 | 탈락 이유 |
|------|-----------|
| SQLite | 파일 기반이지만 스키마 마이그레이션 필요, 현 단계에서 오버엔지니어링 |
| PostgreSQL / Cloud SQL | 인프라 추가 비용, 초기 단계 불필요 |
| Cloud Firestore | 좋은 선택이지만 현재 로컬 에이전트 실행과 Cloud 대시보드가 분리된 구조라 당장 불필요 |
| Python logging (텍스트) | 구조화 안 되어 대시보드 파싱 어려움 |
| JSONL | **채택**: append-only 특성이 로그에 적합, 별도 DB 없이 pandas로 바로 분석 가능 |

## 결과

- **긍정적**: 인프라 의존성 0, 파일 그대로 백업/이동 가능, pandas로 즉시 분석
- **부정적**: 동시 쓰기 안전하지 않음 (에이전트가 단일 프로세스이므로 현재는 문제 없음), 파일 크기 무제한 증가
- **제약**: Cloud Run 컨테이너가 재시작되면 로그 소실. 현재 구조에서는 에이전트가 로컬에서만 실행되므로 문제 없으나, 클라우드 에이전트 실행 전환 시 저장소 이전 필요

## 향후

에이전트를 Cloud Run Jobs로 이전할 때 로그 저장소를 Cloud Storage(JSONL 유지) 또는 Firestore(쿼리 기능 추가)로 전환한다.
