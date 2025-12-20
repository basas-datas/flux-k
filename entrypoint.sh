#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# CUDA 검사 및 설정
echo "Checking CUDA availability..."

# Python을 통한 CUDA 검사
python_cuda_check() {
    python3 -c "
import torch
try:
    if torch.cuda.is_available():
        print('CUDA_AVAILABLE')
        exit(0)
    else:
        print('CUDA_NOT_AVAILABLE')
        exit(1)
except Exception as e:
    print(f'CUDA_ERROR: {e}')
    exit(2)
" 2>/dev/null
}

# CUDA 검사 실행
cuda_status=$(python_cuda_check)
case $? in
    0)
        echo "✅ CUDA is available and working (Python check)"
        export CUDA_VISIBLE_DEVICES=0
        export FORCE_CUDA=1
        ;;
    1)
        echo "❌ CUDA is not available (Python check)"
        echo "Error: CUDA is required but not available. Exiting..."
        exit 1
        ;;
    2)
        echo "❌ CUDA check failed (Python check)"
        echo "Error: CUDA initialization failed. Exiting..."
        exit 1
        ;;
esac

# 추가적인 nvidia-smi 검사
if command -v nvidia-smi &> /dev/null; then
    if nvidia-smi &> /dev/null; then
        echo "✅ NVIDIA driver working (nvidia-smi check)"
    else
        echo "❌ NVIDIA driver found but not working"
        echo "Error: NVIDIA driver is not working properly. Exiting..."
        exit 1
    fi
else
    echo "❌ NVIDIA driver not found"
    echo "Error: NVIDIA driver is required but not found. Exiting..."
    exit 1
fi

# CUDA 환경 변수 설정
echo "Using CUDA device: $CUDA_VISIBLE_DEVICES"

# Start ComfyUI in the background
echo "Starting ComfyUI in the background..."
python /ComfyUI/main.py --listen --use-sage-attention &

# Wait for ComfyUI to be ready
echo "Waiting for ComfyUI to be ready..."
max_wait=120  # 최대 2분 대기
wait_count=0
while [ $wait_count -lt $max_wait ]; do
    if curl -s http://127.0.0.1:8188/ > /dev/null 2>&1; then
        echo "ComfyUI is ready!"
        break
    fi
    echo "Waiting for ComfyUI... ($wait_count/$max_wait)"
    sleep 2
    wait_count=$((wait_count + 2))
done

if [ $wait_count -ge $max_wait ]; then
    echo "Error: ComfyUI failed to start within $max_wait seconds"
    exit 1
fi

echo "Starting the handler..."

if [ "$DEV" = "true" ]; then
    echo "🔥 DEV MODE ENABLED → syncing code from GitHub"

    REPO_URL="https://github.com/basas-datas/flux-k.git"
    RAW_HANDLER_URL="https://raw.githubusercontent.com/basas-datas/flux-k/main/handler.py"

    GIT_OK=true

    if [ -d .git ]; then
        echo "Git repository detected"

        git remote set-url origin "$REPO_URL" || GIT_OK=false

        if [ "$GIT_OK" = "true" ]; then
            git fetch origin || GIT_OK=false
            git reset --hard origin/main || GIT_OK=false
            git clean -fd || GIT_OK=false
        fi
    else
        echo "No git repo found → cloning"
        git clone "$REPO_URL" . || GIT_OK=false
    fi

    if [ "$GIT_OK" != "true" ]; then
        echo "⚠️ Git sync failed → falling back to raw handler.py download"

        curl -fsSL "$RAW_HANDLER_URL" -o handler.py || {
            echo "❌ Fallback failed: could not download handler.py"
            echo "Continuing with existing handler.py"
        }
    fi
else
    echo "🚀 PROD MODE → using baked-in code"
fi

exec python handler.py


