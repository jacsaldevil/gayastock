#!/bin/bash
# GCP 리소스 최초 1회 셋업 스크립트
# 실행: bash scripts/gcp-setup.sh
# 사전 조건: gcloud auth login && gcloud config set project YOUR_PROJECT_ID

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="asia-northeast3"
DASHBOARD_SERVICE="gayastock-dashboard"
AGENT_JOB="gayastock-agent"
SA_NAME="gayastock-runner"

echo "▶ 프로젝트: $PROJECT_ID"

# 1. 필요한 API 활성화
echo "📡 API 활성화 중..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  containerregistry.googleapis.com

# 2. 서비스 계정 생성 (Cloud Run 실행용)
echo "🔑 서비스 계정 생성 중..."
gcloud iam service-accounts create $SA_NAME \
  --display-name="gayastock runner" 2>/dev/null || echo "  이미 존재"

SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.invoker" --quiet

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" --quiet

# 3. GitHub Actions용 서비스 계정 키 생성
echo "🔐 GitHub Actions용 SA 키 생성 중..."
gcloud iam service-accounts create "github-actions" \
  --display-name="GitHub Actions deployer" 2>/dev/null || echo "  이미 존재"

GA_SA="github-actions@$PROJECT_ID.iam.gserviceaccount.com"

for ROLE in roles/run.admin roles/iam.serviceAccountUser roles/storage.admin roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$GA_SA" --role="$ROLE" --quiet
done

gcloud iam service-accounts keys create /tmp/github-actions-key.json \
  --iam-account=$GA_SA

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 GitHub Secrets에 아래 값들을 등록하세요"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "GCP_PROJECT_ID = $PROJECT_ID"
echo "GCP_SA_KEY     = $(cat /tmp/github-actions-key.json | base64 -w 0)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
rm /tmp/github-actions-key.json

# 4. Secret Manager에 API 키 등록
echo ""
echo "🔒 Secret Manager에 API 키를 등록합니다..."
for SECRET in GOOGLE_API_KEY KIS_APP_KEY KIS_APP_SECRET KIS_ACCOUNT_NO; do
  if ! gcloud secrets describe "$SECRET" --quiet &>/dev/null; then
    printf "  %s 값 입력: " "$SECRET"
    read -rs VALUE; echo
    printf '%s' "$VALUE" | gcloud secrets create "$SECRET" --data-file=-
  else
    echo "  $SECRET 이미 존재 (건너뜀)"
  fi
done

# 5. Cloud Run Jobs 초기 생성 (이미지는 임시값, Actions에서 업데이트)
echo ""
echo "🤖 Cloud Run Job 초기 생성 중..."
gcloud run jobs create $AGENT_JOB \
  --image "gcr.io/cloudrun/placeholder" \
  --region $REGION \
  --service-account $SA_EMAIL \
  --memory 512Mi \
  --cpu 1 \
  --task-timeout 600 2>/dev/null || echo "  이미 존재"

# 6. Cloud Scheduler 등록 (평일 09:10, 14:00 KST)
echo "⏰ Cloud Scheduler 등록 중..."
gcloud scheduler jobs create http "${AGENT_JOB}-morning" \
  --location $REGION \
  --schedule "10 9 * * 1-5" \
  --time-zone "Asia/Seoul" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${AGENT_JOB}:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL 2>/dev/null || echo "  morning 이미 존재"

gcloud scheduler jobs create http "${AGENT_JOB}-afternoon" \
  --location $REGION \
  --schedule "0 14 * * 1-5" \
  --time-zone "Asia/Seoul" \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${AGENT_JOB}:run" \
  --http-method POST \
  --oauth-service-account-email $SA_EMAIL 2>/dev/null || echo "  afternoon 이미 존재"

echo ""
echo "✅ GCP 초기 셋업 완료!"
echo "   이제 main 브랜치에 push하면 GitHub Actions가 자동 배포합니다."
