#!/usr/bin/env bash
set -euo pipefail

# 기본값 (필요 시 수정)
NAME=${NAME:-devops-lab}
REGION=${REGION:-ap-northeast-2}
ACCOUNT_ID=${ACCOUNT_ID:-651706765732}   # 본인 AWS 계정 ID로 수정

echo "[1/4] 베이스 인프라 생성"
python3 infra/main.py up --name "$NAME" --region "$REGION"

echo "[2/4] SageMaker 온보딩 (전용 Role + Notebook)"
python3 infra/sagemaker.py up --name "$NAME" --region "$REGION"

echo "[3/4] 학습 데이터 업로드"
aws s3 cp ./data/train.csv "s3://${NAME}-${ACCOUNT_ID}-bucket/train.csv" --region "$REGION"

echo "[4/4] SageMaker 학습 → 배포"
python3 infra/sagemaker_train_deploy.py

echo "[완료] 모든 리소스가 준비되었습니다."
