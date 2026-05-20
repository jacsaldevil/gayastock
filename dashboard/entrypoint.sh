#!/bin/sh
set -e

cp /app/dashboard/nginx.conf /etc/nginx/nginx.conf

# FastAPI 로그 API (포트 8001) — 백그라운드
uvicorn dashboard.api_server:app --host 127.0.0.1 --port 8001 &

# Streamlit 대시보드 (포트 8501) — 백그라운드로 먼저 시작
streamlit run dashboard/app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --server.enableCORS=false &

# Streamlit이 포트 8501을 열 때까지 대기 (최대 60초)
i=0
until python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', 8501)); s.close()" 2>/dev/null; do
    i=$((i+1))
    [ $i -ge 60 ] && echo "Streamlit 시작 타임아웃" && break
    sleep 1
done

# Streamlit 준비 완료 후 nginx를 포어그라운드로 실행 (PID 1)
exec nginx -g "daemon off;"
