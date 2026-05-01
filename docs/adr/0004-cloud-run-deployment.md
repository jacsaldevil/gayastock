# ADR-004: Google Cloud Run 배포

- **상태**: Accepted
- **날짜**: 2026-05-01

## 배경

대시보드를 로컬이 아닌 외부에서 접근 가능한 URL로 서빙해야 했다. 개발 환경 자체는 공개 IP가 없어 클라우드 배포가 필요했다.

## 결정

**대시보드(Streamlit)를 Google Cloud Run에 배포한다.**

아키텍처:
```
로컬 PC
└── python main.py   ← 에이전트 실행 (KIS API 직접 호출)

Google Cloud Run
└── dashboard/app.py ← 대시보드 (KIS API 잔고 조회 + 로그 파일 읽기)
```

인증/비밀 관리는 Cloud Secret Manager를 사용하고, `.env` 파일은 컨테이너에 포함하지 않는다.

## 검토한 대안

| 방식 | 탈락 이유 |
|------|-----------|
| Streamlit Community Cloud | KIS API 키 보안 관리 어려움, 커스텀 도메인 제한 |
| Cloud Compute Engine (VM) | 항상 켜져 있어 비용 발생, 관리 오버헤드 |
| Railway / Render | GCP 생태계 외부, Secret Manager 미지원 |
| Cloud Run | **채택**: 트래픽 없을 때 인스턴스 0으로 축소 → 비용 최소화 |

## 배포 구성

| 항목 | 설정값 | 이유 |
|------|--------|------|
| 리전 | `asia-northeast3` (서울) | 레이턴시 최소화 |
| 메모리 | 512Mi | Pandas/Plotly 로딩 여유분 |
| min-instances | 0 | 비용 절감 (콜드스타트 감수) |
| max-instances | 2 | 개인 대시보드 수준 트래픽 |
| 인증 | `--allow-unauthenticated` | 개인 접근 용도, 필요 시 IAP로 교체 |

## 결과

- **긍정적**: 요청 없을 때 비용 0에 수렴, Secret Manager로 키 안전 관리, 빌드 자동화(`deploy.sh`)
- **부정적**: 콜드스타트 시 초기 응답 3~5초 지연 가능
- **제약**: 에이전트(`main.py`)는 Cloud Run에서 실행하지 않음. 컨테이너 재시작 시 로컬 JSONL 로그가 사라지므로, 에이전트-대시보드 간 로그 공유는 현재 로컬 전용

## 향후

에이전트도 클라우드에서 스케줄 실행하려면 Cloud Scheduler + Cloud Run Jobs로 전환하고, 로그 저장소를 Cloud Storage 또는 Firestore로 이전한다.
