# scripts/submit_sm_training.py
import os, time, json, boto3, pathlib, sys
from sagemaker import image_uris

region = os.environ["AWS_REGION"]
bucket = os.environ["MODEL_BUCKET"]        # e.g. devops-lab-651706765732-ml
repo   = os.environ.get("MODEL_REPO","devops-lab")
exec_role = os.environ["SM_TRAIN_EXEC_ROLE_ARN"]  # TF output
out_prefix = f"s3://{bucket}/sm-output/"
code_s3    = f"s3://{bucket}/sm-code/"

sm = boto3.client("sagemaker", region_name=region)
s3 = boto3.client("s3", region_name=region)

# 1) 학습 스크립트 업로드(간단: train_sm.py만)
src = "scripts/train_sm.py"
key = f"sm-code/{repo}/train_sm.py"
s3.upload_file(src, bucket, key)

# 2) 컨테이너 이미지(Scikit-learn) URI
image_uri = image_uris.retrieve(framework="sklearn", region=region, version="1.2-1", py_version="py3")

# 3) TrainingJob 생성
job_name = f"{repo}-train-{int(time.time())}"
resp = sm.create_training_job(
    TrainingJobName=job_name,
    AlgorithmSpecification={
        "TrainingImage": image_uri,
        "TrainingInputMode": "File",
        "MetricDefinitions": [{"Name":"train:done","Regex":".*saved:.*"}],
        "EnableSageMakerMetricsTimeSeries": False
    },
    RoleArn=exec_role,
    OutputDataConfig={"S3OutputPath": out_prefix},
    ResourceConfig={"InstanceType":"ml.m5.large","InstanceCount":1,"VolumeSizeInGB":10},
    StoppingCondition={"MaxRuntimeInSeconds": 1800},
    HyperParameters={"sagemaker_program":"train_sm.py","sagemaker_submit_directory":f"s3://{bucket}/{key}"},
    InputDataConfig=[],
    EnableNetworkIsolation=False
)

# 4) 대기
while True:
    desc = sm.describe_training_job(TrainingJobName=job_name)
    status = desc["TrainingJobStatus"]
    if status in ("Completed","Failed","Stopped"):
        break
    time.sleep(10)

print(json.dumps({"job_name":job_name,"status":status,"model_artifacts":desc["ModelArtifacts"]["S3ModelArtifacts"]}))
if status != "Completed":
    sys.exit(2)
