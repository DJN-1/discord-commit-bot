#!/bin/bash
set -e
echo "▶️ start.sh 시작됨"

echo "▶️ ping_server.py 실행 중..."
python3 ping_server.py &

echo "▶️ main.py 실행 시작"
python3 main.py

echo "✅ main.py 종료됨 (이 메시지가 보이면 예상치 못한 종료 발생)"
