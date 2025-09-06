# infra/sagemaker_train_deploy.py
# 사용법:
#   python3 infra/sagemaker_train_deploy.py
#
# 동작:
#  - infra_info.json에서 s3_bucket, (sagemaker_role_arn or iam_role_arn), subnet_ids, svc_sg_id 읽음
#  - XGBoost 트레이닝 → 모델 생성 → 엔드포인트 구성/생성 → InService 대기
#
# 사전 준비:
#  - infra/main.py up, infra/sagemaker.py up 실행으로 infra_info.json 및 sagemaker_role_arn 확보
#  - S3 버킷에 train.csv 업로드 (예: s3://<bucket>/train.csv)

import os
import json
import time
import boto3
from botocore.exceptions import ClientError

INFRA_FILE = os.path.join(os.path.dirname(__file__), "..", "infra_info.json")

# 리전별 XGBoost 공식 컨테이너 계정 매핑(필요 시 확장)
XGB_ACCOUNT_BY_REGION = {
    "ap-northeast-2": "382416733822",
    "us-east-1":      "811284229777",
    "us-west-2":      "433757028032",
    "eu-west-1":      "685385470294",
}
XGB_REPO = "xgboost:latest"

def load_infra_info(path=INFRA_FILE):
    if not os.path.exists(path):
        raise FileNotFoundError(f"[ERROR] infra_info.json이 없습니다: {path}\n먼저 infra/main.py up → infra/sagemaker.py up을 실행하세요.")
    with open(path) as f:
        return json.load(f)

def resolve_region():
    # boto3 기본 동작에 맡기되, 환경변수 없으면 ap-northeast-2
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-2"

def xgb_image_uri(region):
    acct = XGB_ACCOUNT_BY_REGION.get(region)
    if not acct:
        # 알 수 없는 리전이면 us-west-2로 폴백
        acct = XGB_ACCOUNT_BY_REGION["us-west-2"]
        region = "us-west-2"
    return f"{acct}.dkr.ecr.{region}.amazonaws.com/{XGB_REPO}"

def create_training_job(sagemaker, job_name, role_arn, s3_input, s3_output, image_uri):
    resp = sagemaker.create_training_job(
        TrainingJobName=job_name,
        AlgorithmSpecification={
            "TrainingImage": image_uri,
            "TrainingInputMode": "File"
        },
        RoleArn=role_arn,
        InputDataConfig=[{
            "ChannelName": "train",
            "DataSource": {
                "S3DataSource": {
                    "S3DataType": "S3Prefix",
                    "S3Uri": s3_input,
                    "S3DataDistributionType": "FullyReplicated"
                }
            },
            "ContentType": "csv"
        }],
        OutputDataConfig={"S3OutputPath": s3_output},
        ResourceConfig={"InstanceType": "ml.m5.large", "InstanceCount": 1, "VolumeSizeInGB": 5},
        StoppingCondition={"MaxRuntimeInSeconds": 3600}
    )
    print(f"[INFO] Training job '{job_name}' started.")
    return resp

def wait_for_training_job(sagemaker, job_name):
    while True:
        desc = sagemaker.describe_training_job(TrainingJobName=job_name)
        status = desc["TrainingJobStatus"]
        sec = desc.get("SecondaryStatus", "")
        print(f"[INFO] Training job status: {status} ({sec})")
        if status in ["Completed", "Failed", "Stopped"]:
            break
        time.sleep(30)
    return status

def create_model(sagemaker, model_name, role_arn, model_artifact, image_uri, subnet_ids=None, sg_id=None):
    params = {
        "ModelName": model_name,
        "PrimaryContainer": {
            "Image": image_uri,
            "ModelDataUrl": model_artifact
        },
        "ExecutionRoleArn": role_arn
    }
    if subnet_ids and sg_id:
        params["VpcConfig"] = {
            "Subnets": subnet_ids,
            "SecurityGroupIds": [sg_id]
        }
    resp = sagemaker.create_model(**params)
    print(f"[INFO] Model '{model_name}' created.")
    return resp

def create_endpoint_config(sagemaker, config_name, model_name):
    resp = sagemaker.create_endpoint_config(
        EndpointConfigName=config_name,
        ProductionVariants=[{
            "VariantName": "AllTraffic",
            "ModelName": model_name,
            "InstanceType": "ml.m5.large",
            "InitialInstanceCount": 1
        }]
    )
    print(f"[INFO] Endpoint config '{config_name}' created.")
    return resp

def create_endpoint(sagemaker, endpoint_name, config_name):
    resp = sagemaker.create_endpoint(
        EndpointName=endpoint_name,
        EndpointConfigName=config_name
    )
    print(f"[INFO] Endpoint '{endpoint_name}' creation started.")
    return resp

def wait_for_endpoint(sagemaker, endpoint_name):
    while True:
        desc = sagemaker.describe_endpoint(EndpointName=endpoint_name)
        status = desc["EndpointStatus"]
        print(f"[INFO] Endpoint status: {status}")
        if status in ["InService", "Failed"]:
            break
        time.sleep(30)
    return status

if __name__ == "__main__":
    infra = load_infra_info()
    region = resolve_region()

    s3_bucket = infra.get("s3_bucket")
    if not s3_bucket:
        raise RuntimeError("[ERROR] infra_info.json에 's3_bucket'이 없습니다.")
    s3_input  = f"s3://{s3_bucket}/train.csv"
    s3_output = f"s3://{s3_bucket}/output/"

    # ✅ 역할 우선순위: sagemaker_role_arn > iam_role_arn
    role_arn = infra.get("sagemaker_role_arn") or infra.get("iam_role_arn")
    if not role_arn:
        raise RuntimeError("[ERROR] infra_info.json에 'sagemaker_role_arn' 또는 'iam_role_arn'이 없습니다.")

    subnet_ids = infra.get("subnet_ids") or []
    sg_id = infra.get("svc_sg_id")

    ts = time.strftime("%Y%m%d-%H%M%S")
    base = (infra.get("ecs_service") or "dspm-demo").replace("_", "-")
    job_name      = f"{base}-train-{ts}"
    model_name    = f"{base}-model-{ts}"
    config_name   = f"{base}-cfg-{ts}"
    endpoint_name = f"{base}-ep"  # 엔드포인트는 고정 이름 권장(업데이트 용이)

    image_uri = xgb_image_uri(region)
    print(f"[INFO] region={region}")
    print(f"[INFO] image_uri={image_uri}")
    print(f"[INFO] s3_input={s3_input}")
    print(f"[INFO] s3_output={s3_output}")
    print(f"[INFO] role_arn={role_arn}")
    if subnet_ids and sg_id:
        print(f"[INFO] VPC deploy on: subnets={subnet_ids}, sg={sg_id}")
    else:
        print("[INFO] VPC 정보가 없어 퍼블릭 엔드포인트로 생성됩니다.")

    sagemaker = boto3.client("sagemaker", region_name=region)

    # 1) Train
    try:
        create_training_job(sagemaker, job_name, role_arn, s3_input, s3_output, image_uri)
    except ClientError as e:
        raise RuntimeError(f"[ERROR] create_training_job 실패: {e}") from e

    status = wait_for_training_job(sagemaker, job_name)
    if status != "Completed":
        print("[ERROR] Training job failed or stopped.")
        raise SystemExit(1)

    # 2) Model
    job_desc = sagemaker.describe_training_job(TrainingJobName=job_name)
    model_artifact = job_desc["ModelArtifacts"]["S3ModelArtifacts"]
    try:
        create_model(
            sagemaker, model_name, role_arn, model_artifact, image_uri,
            subnet_ids=subnet_ids if subnet_ids else None,
            sg_id=sg_id
        )
    except ClientError as e:
        raise RuntimeError(f"[ERROR] create_model 실패: {e}") from e

    # 3) Endpoint
    try:
        create_endpoint_config(sagemaker, config_name, model_name)
        create_endpoint(sagemaker, endpoint_name, config_name)
    except ClientError as e:
        raise RuntimeError(f"[ERROR] 엔드포인트 생성 단계 실패: {e}") from e

    ep_status = wait_for_endpoint(sagemaker, endpoint_name)
    if ep_status != "InService":
        raise SystemExit("[ERROR] Endpoint 생성 실패")
    print("[INFO] SageMaker Endpoint 준비 완료.")
