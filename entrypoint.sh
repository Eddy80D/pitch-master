#!/bin/bash
# Sealos 发布版启动脚本（应用管理托管容器入口）
cd "$(dirname "$0")"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
