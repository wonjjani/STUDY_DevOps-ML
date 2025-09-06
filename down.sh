#!/usr/bin/env bash
set -euo pipefail

# 기본값 (필요 시 수정)
NAME=${NAME:-devops-lab}
REGION=${REGION:-ap-northeast-2}

echo "[1/2] SageMaker Notebook 종료 및 삭제"
python3 infra/sagemaker.py down --name "$NAME" --region "$REGION"

echo "[2/2] 베이스 인프라 삭제"
python3 infra/main.py down --name "$NAME" --region "$REGION"

echo "[완료] 모든 리소스를 정리했습니다."
