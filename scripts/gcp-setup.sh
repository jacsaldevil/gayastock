#!/bin/bash
# GCP 리소스 최초 1회 셋업 스크립트 (Workload Identity Federation 방식)
# SA 키 생성 없이 GitHub Actions 인증 — 조직 정책 우회
# 실행: bash scripts/gcp-setup.sh YOUR_GITHUB_REPO (예: jacsaldevil/gayastock)

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="asia-northeast3"
DASHBOARD_SERVICE="gayastock-dashboard"
AGENT_JOB="gayastock-agent"
SA_NAME="gayastock-runner"
GA_SA_NAME="github-actions"
GITHUB_REPO="${1:-jacsaldevil/gayastock}"

echo "▶ 프로젝트: $PROJECT_ID"
echo "▶ GitHub 레포: $GITHUB_REPO"

# 1. API 활성화
echo ""
echo "📡 API 활성화 중..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  containerregistry.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --quiet

# 2. Cloud Run 실행용 서비스 계정
echo ""
echo "🔑 서비스 계정 생성 중..."
gcloud iam service-accounts create $SA_NAME \
  --display-name="gayastock runner" --quiet 2>/dev/null || echo "  $SA_NAME 이미 존재"

SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

for ROLE in roles/run.invoker roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" --role="$ROLE" --quiet
done

# 3. GitHub Actions용 서비스 계정 (키 없음)
gcloud iam service-accounts create $GA_SA_NAME \
  --display-name="GitHub Actions deployer" --quiet 2>/dev/null || echo "  $GA_SA_NAME 이미 존재"

GA_SA="$GA_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

for ROLE in roles/run.admin roles/iam.serviceAccountUser roles/storage.admin roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$GA_SA" --role="$ROLE" --quiet
done

# 4. Workload Identity Pool & Provider 생성
echo ""
echo "🔐 Workload Identity Federation 설정 중..."

gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions Pool" --quiet 2>/dev/null || echo "  pool 이미 존재"

POOL_ID=$(gcloud iam workload-identity-pools describe "github-pool" \
  --location="global" --format="value(name)")

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub Provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.actor=assertion.actor" \
  --issuer-uri="https://token.actions.githubusercontent.com" --quiet 2>/dev/null || echo "  provider 이미 존재"

# GitHub 레포에서 SA 사칭 허용
gcloud iam service-accounts add-iam-policy-binding $GA_SA \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${GITHUB_REPO}" \
  --quiet

PROVIDER_NAME=$(gcloud iam workload-identity-pools providers describe "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)")

# 5. Secret Manager에 API 키 등록
echo ""
echo "🔒 Secret Manager 등록 중..."
for SECRET in GOOGLE_API_KEY KIS_APP_KEY KIS_APP_SECRET KIS_ACCOUNT_NO; do
  if ! gcloud secrets describe "$SECRET" --quiet &>/dev/null; then
    printf "  %s 값 입력: " "$SECRET"
    read -rs VALUE; echo
    printf '%s' "$VALUE" | gcloud secrets create "$SECRET" --data-file=- --quiet
  else
    echo "  $SECRET 이미 존재 (건너뜀)"
  fi
done

# 6. Cloud Run Jobs 초기 생성
echo ""
echo "🤖 Cloud Run Job 생성 중..."
gcloud run jobs create $AGENT_JOB \
  --image "gcr.io/cloudrun/placeholder" \
  --region $REGION \
  --service-account $SA_EMAIL \
  --memory 512Mi \
  --cpu 1 \
  --task-timeout 600 --quiet 2>/dev/null || echo "  이미 존재"

# 7. Cloud Scheduler 등록
echo "⏰ Cloud Scheduler 등록 중..."
SCHEDULER_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${AGENT_JOB}:run"
SCHEDULER_FLAGS="--location $REGION --time-zone Asia/Seoul --uri $SCHEDULER_URI --http-method POST --oauth-service-account-email $SA_EMAIL --quiet"

gcloud scheduler jobs create http "${AGENT_JOB}-morning" \
  $SCHEDULER_FLAGS --schedule "10 9 * * 1-5" 2>/dev/null || echo "  morning 이미 존재"

gcloud scheduler jobs create http "${AGENT_JOB}-midday" \
  $SCHEDULER_FLAGS --schedule "0 12 * * 1-5" 2>/dev/null || echo "  midday 이미 존재"

gcloud scheduler jobs create http "${AGENT_JOB}-afternoon" \
  $SCHEDULER_FLAGS --schedule "30 14 * * 1-5" 2>/dev/null || echo "  afternoon 이미 존재"

# 결과 출력
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 셋업 완료! GitHub Secrets에 아래 2개만 등록하세요"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "GCP_PROJECT_ID          = $PROJECT_ID"
echo ""
echo "WIF_PROVIDER            = $PROVIDER_NAME"
echo ""
echo "WIF_SERVICE_ACCOUNT     = $GA_SA"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
