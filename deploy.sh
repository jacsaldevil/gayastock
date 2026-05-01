#!/bin/bash
# Google Cloud Run 배포 스크립트
# 사전 준비: gcloud auth login && gcloud config set project YOUR_PROJECT_ID

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="asia-northeast3"        # 서울 리전
SERVICE="gayastock-dashboard"
IMAGE="gcr.io/$PROJECT_ID/$SERVICE"

echo "▶ 프로젝트: $PROJECT_ID"
echo "▶ 리전: $REGION"
echo "▶ 서비스: $SERVICE"

# 1. Secret Manager에 환경변수 등록 (최초 1회)
setup_secrets() {
  echo "🔐 Secret Manager 등록 중..."
  for SECRET in ANTHROPIC_API_KEY KIS_APP_KEY KIS_APP_SECRET KIS_ACCOUNT_NO; do
    if ! gcloud secrets describe "$SECRET" &>/dev/null; then
      echo -n "  $SECRET 값 입력: "
      read -rs VALUE
      echo
      echo -n "$VALUE" | gcloud secrets create "$SECRET" --data-file=-
    else
      echo "  $SECRET 이미 존재 (건너뜀)"
    fi
  done
}

# 2. Docker 이미지 빌드 & 푸시
build_and_push() {
  echo "🐳 이미지 빌드 중..."
  gcloud builds submit --tag "$IMAGE" .
}

# 3. Cloud Run 배포
deploy() {
  echo "🚀 Cloud Run 배포 중..."
  gcloud run deploy "$SERVICE" \
    --image "$IMAGE" \
    --platform managed \
    --region "$REGION" \
    --allow-unauthenticated \
    --memory 512Mi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 2 \
    --set-env-vars "KIS_MOCK=true,MAX_BUY_AMOUNT=500000,MAX_POSITIONS=5" \
    --set-secrets \
      "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,\
KIS_APP_KEY=KIS_APP_KEY:latest,\
KIS_APP_SECRET=KIS_APP_SECRET:latest,\
KIS_ACCOUNT_NO=KIS_ACCOUNT_NO:latest"

  URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format "value(status.url)")
  echo ""
  echo "✅ 배포 완료!"
  echo "🌐 URL: $URL"
}

# 인수 처리
case "${1:-all}" in
  secrets) setup_secrets ;;
  build)   build_and_push ;;
  deploy)  deploy ;;
  all)
    setup_secrets
    build_and_push
    deploy
    ;;
  *)
    echo "사용법: ./deploy.sh [secrets|build|deploy|all]"
    ;;
esac
