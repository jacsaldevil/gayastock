# ADR-002: KIS 내장 재무제표 API 채택 (DART 제거)

- **상태**: Accepted
- **날짜**: 2026-05-01

## 배경

재무제표 데이터 수집 방법으로 초기에 두 가지를 검토했다.

- **DART API** (금융감독원 전자공시): 별도 API 키 필요, 고유번호(corp_code) 변환 과정 필요
- **KIS 내장 재무 API**: KIS Open API 자체에 재무제표 엔드포인트 존재

초기 구현에서는 DART API를 사용했으나, 공식 레포지토리(`koreainvestment/open-trading-api`)를 분석한 결과 KIS API 자체에 재무 엔드포인트가 내장되어 있음을 확인했다.

## 결정

**KIS API 내장 재무 엔드포인트를 사용하고 DART API 의존성을 제거한다.**

사용 엔드포인트:

| 엔드포인트 | TR ID | 데이터 |
|-----------|-------|--------|
| `/uapi/domestic-stock/v1/finance/income-statement` | `FHKST66430200` | 매출액, 영업이익, 당기순이익, EPS |
| `/uapi/domestic-stock/v1/finance/balance-sheet` | `FHKST66430100` | 자산, 부채, 자본, 부채비율 |
| `/uapi/domestic-stock/v1/finance/financial-ratio` | `FHKST66430300` | ROE, 영업이익률, 순이익률, PER, PBR |

## 검토한 대안

| 방법 | 탈락 이유 |
|------|-----------|
| DART OpenAPI | 별도 API 키 필요, 종목코드→corp_code 변환 레이어 필요, 응답 파싱 복잡 |
| yfinance | 국내 주식 재무데이터 품질 낮음, 비공식 스크래핑 |
| KIS 내장 API | **채택**: 이미 보유한 KIS 키 재사용, 변환 레이어 불필요 |

## 결과

- **긍정적**: API 키 2개(KIS + Anthropic)만으로 전체 시스템 동작, DART corp_code 변환 로직 제거
- **부정적**: KIS에서 제공하는 재무 데이터 범위로 제한됨 (감사보고서 원문 등 심층 데이터는 여전히 DART 필요)
- **향후**: 심층 분석이 필요할 경우 DART API를 선택적으로 추가할 수 있음
