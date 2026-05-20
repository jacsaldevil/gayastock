#!/bin/sh
set -e

# nginx 설정 복사 & 시작
cp /app/dashboard/nginx.conf /etc/nginx/nginx.conf
nginx

# FastAPI 로그 API (포트 8001)
uvicorn dashboard.api_server:app --host 127.0.0.1 --port 8001 &

# Streamlit 대시보드 (포트 8501)
exec streamlit run dashboard/app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --server.enableCORS=false
