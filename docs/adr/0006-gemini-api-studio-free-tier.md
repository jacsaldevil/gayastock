# ADR-006: Gemini AI Studio 무료 티어 사용 (Google One Pro 구독 미활용)

- **상태**: Accepted
- **날짜**: 2026-05-01

## 배경

트레이딩 에이전트 AI를 Claude API에서 Gemini로 전환하기로 결정했다.
Claude Pro 플랜의 토큰을 자동화된 에이전트에 소비하지 않는 것이 목표였다.

초기에는 보유 중인 Google One AI Premium(Google Pro 구독)을 에이전트에 활용하는 방안을 검토했다.

## 조사 결과

**Google One AI Premium 구독은 Gemini API 접근 권한을 포함하지 않는다.**

| 제품 | 용도 | API 코드 호출 |
|------|------|--------------|
| Google One AI Premium | Gmail·Docs 내 Gemini Advanced 채팅 | ❌ |
| Google AI Studio API | 코드에서 직접 Gemini 호출 | ✅ |

두 제품은 완전히 별개이며, 구독료가 API 크레딧으로 전환되지 않는다.

## 결정

**Google AI Studio에서 별도 API 키를 발급받아 무료 티어로 사용한다.**

무료 티어 한도 (Gemini 2.0 Flash 기준):
- 분당 15 요청
- 일 1,500 요청
- 컨텍스트 윈도우 1M 토큰

트레이딩 에이전트는 하루 2회 실행, 1회 실행 시 약 10~15회 API 호출이므로
일 30회 수준 → **무료 티어로 충분하다.**

## 역할 분리

| 도구 | 역할 | 비용 |
|------|------|------|
| Claude Code Pro | 코드 작성, 배포, 서버 관리 | 기존 Pro 플랜 활용 |
| Gemini API (AI Studio) | 트레이딩 에이전트 AI 판단 | 무료 티어 |
| Google Cloud | 인프라 (Cloud Run, Scheduler, Storage) | 사용량 기반 소액 |

## 결과

- **긍정적**: Claude Pro 토큰 소모 없음, Gemini API 무료 운영 가능
- **부정적**: Google One Pro 구독이 API에는 미활용 (채팅 용도로만 사용됨)
- **향후**: 트래픽 증가 또는 더 강력한 모델(Gemini 1.5 Pro) 필요 시 AI Studio 유료 플랜 전환
