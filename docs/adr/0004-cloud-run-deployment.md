# ADR-004: Google Cloud Run 배포 + GitHub Actions CI/CD

- **상태**: Accepted (Updated 2026-05-01)
- **날짜**: 2026-05-01

## 배경

대시보드와 에이전트를 GCP에서 실행해야 하며, 코드 변경 시 자동으로 배포되는 파이프라인이 필요하다.

## 결정

**대시보드는 Cloud Run, 에이전트는 Cloud Run Jobs로 배포하고, CI/CD는 GitHub Actions로 자동화한다.**

```
main 브랜치 push
  └── GitHub Actions
        ├── Docker 빌드 (대시보드 / 에이전트 분리)
        ├── GCR 푸시
        ├── Cloud Run 배포 (대시보드)
        └── Cloud Run Jobs 업데이트 (에이전트)

Cloud Scheduler (평일 09:10 / 14:00 KST)
  └── Cloud Run Jobs 실행 (에이전트 1회 실행 후 종료)
```

## 컨테이너 분리

| 파일 | 대상 | 진입점 |
|------|------|--------|
| `Dockerfile` | Cloud Run (대시보드) | `streamlit run dashboard/app.py` |
| `Dockerfile.agent` | Cloud Run Jobs (에이전트) | `python main.py --once` |

## CI/CD 구성 (GitHub Actions)

**필요한 GitHub Secrets:**

| Secret | 설명 |
|--------|------|
| `GCP_PROJECT_ID` | GCP 프로젝트 ID |
| `GCP_SA_KEY` | GitHub Actions용 서비스 계정 JSON 키 (base64) |

앱 비밀(KIS 키, Gemini 키)은 GCP Secret Manager에 저장하고 컨테이너 실행 시 주입한다. 코드 저장소에는 포함하지 않는다.

## 검토한 대안

| 방식 | 탈락 이유 |
|------|-----------|
| Cloud Build 트리거 | GitHub Actions로 통합 가능, 별도 도구 불필요 |
| 수동 `deploy.sh` | 자동화 안 됨, 실수 여지 있음 |
| GitHub Actions | **채택**: 코드 리뷰·배포 파이프라인 한 곳에서 관리 |

## 초기 설정

GCP 리소스(Cloud Run Job, Scheduler, Secret Manager, 서비스 계정)는 `scripts/gcp-setup.sh`로 최초 1회 생성한다. 이후 변경사항은 GitHub Actions가 자동 처리한다.

## 배포 구성

| 항목 | 설정 | 이유 |
|------|------|------|
| 리전 | `asia-northeast3` | 서울, KIS API 레이턴시 최소화 |
| 대시보드 메모리 | 512Mi | Pandas/Plotly 여유분 |
| 에이전트 timeout | 600s | 재무 분석 + 주문 실행 여유 |
| min-instances | 0 | 비용 절감 |

## 결과

- **긍정적**: git push 하나로 배포 완결, Secret Manager로 키 안전 관리, Claude Code에서 gcloud 없이도 배포 가능
- **부정적**: GitHub Actions 초기 설정 필요 (SA 키, Secrets 등록)
- **제약**: Cloud Run Jobs는 최초 생성 시 `gcp-setup.sh` 실행 필요 (GitHub Actions는 update만 수행)
