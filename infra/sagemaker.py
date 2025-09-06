# infra/sagemaker.py
# 사용법:
#   python3 infra/sagemaker.py up   --name devops-lab --region ap-northeast-2
#   python3 infra/sagemaker.py down --name devops-lab --region ap-northeast-2
#
# 기능:
#  - SageMaker 전용 Execution Role 없으면 생성(+필수 정책 부착)
#  - 생성한 Role ARN을 infra_info.json에 'sagemaker_role_arn' 키로 추가 기록
#  - SageMaker Notebook 생성/삭제
#  - SageMaker Endpoint/Config/Model 삭제

import argparse
import json
import os
import time
import boto3
from botocore.exceptions import ClientError

INFRA_INFO_PATH = os.path.join(os.path.dirname(__file__), "..", "infra_info.json")
SM_INFO_PATH = "sagemaker_info.json"

def load_infra_json(filename=INFRA_INFO_PATH):
    if not os.path.exists(filename):
        return None
    with open(filename) as f:
        return json.load(f)

def upsert_infra_fields(new_fields: dict, filename=INFRA_INFO_PATH):
    """infra_info.json 병합 업데이트"""
    data = {}
    if os.path.exists(filename):
        try:
            with open(filename) as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.update({k: v for k, v in new_fields.items() if v is not None})
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[INFO] infra_info.json 업데이트: {list(new_fields.keys())}")

def ensure_sagemaker_role(iam, name, infra):
    if infra and infra.get("sagemaker_role_arn"):
        print(f"[INFO] 기존 sagemaker_role_arn 사용: {infra['sagemaker_role_arn']}")
        return infra["sagemaker_role_arn"]

    role_name = f"{name}-sagemaker-exec"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "sagemaker.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }

    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        print(f"[INFO] SageMaker Execution Role 발견: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            print(f"[INFO] SageMaker Execution Role 생성: {role_name}")
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Description="Execution role for SageMaker training & deployment",
            )
            role = iam.get_role(RoleName=role_name)["Role"]
        else:
            raise

    policy_arns = [
        "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    ]
    try:
        attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        attached_set = {p["PolicyArn"] for p in attached}
    except ClientError:
        attached_set = set()

    for arn in policy_arns:
        if arn not in attached_set:
            try:
                iam.attach_role_policy(RoleName=role_name, PolicyArn=arn)
            except ClientError as e:
                print(f"[WARN] 정책 부착 실패({arn}): {e}")

    role_arn = role["Arn"]
    upsert_infra_fields({"sagemaker_role_arn": role_arn})
    return role_arn

def get_default_subnet_and_sg(infra):
    subnet_id = infra["subnet_ids"][0] if infra and "subnet_ids" in infra and infra["subnet_ids"] else None
    sg_id = infra["svc_sg_id"] if infra and "svc_sg_id" in infra else None
    return subnet_id, sg_id

def create_sagemaker_notebook(sm, name, role_arn, subnet_id, sg_id):
    notebook_name = f"{name}-notebook"
    params = {
        "NotebookInstanceName": notebook_name,
        "InstanceType": "ml.t3.medium",
        "RoleArn": role_arn,
    }
    if subnet_id and sg_id:
        params["SubnetId"] = subnet_id
        params["SecurityGroupIds"] = [sg_id]

    try:
        sm.create_notebook_instance(**params)
        print(f"[INFO] Notebook 인스턴스 생성: {notebook_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUse":
            print(f"[INFO] 이미 존재하는 Notebook 인스턴스: {notebook_name}")
        else:
            raise
    return notebook_name

def wait_for_notebook_status(sm, notebook_name, target_status, timeout=600, interval=10):
    start = time.time()
    while True:
        status = sm.describe_notebook_instance(NotebookInstanceName=notebook_name)["NotebookInstanceStatus"]
        print(f"[INFO] Notebook 상태: {status}")
        if status == target_status:
            break
        if time.time() - start > timeout:
            raise TimeoutError(f"Notebook {notebook_name} 상태 {target_status} 대기 타임아웃")
        time.sleep(interval)

def delete_sagemaker_notebook(sm, notebook_name):
    try:
        desc = sm.describe_notebook_instance(NotebookInstanceName=notebook_name)
        status = desc["NotebookInstanceStatus"]
        if status == "InService":
            sm.stop_notebook_instance(NotebookInstanceName=notebook_name)
            print(f"[INFO] Notebook 중지 요청: {notebook_name}")
            wait_for_notebook_status(sm, notebook_name, "Stopped", timeout=600)
        sm.delete_notebook_instance(NotebookInstanceName=notebook_name)
        print(f"[INFO] Notebook 삭제 요청: {notebook_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFound":
            print(f"[INFO] 이미 삭제된 Notebook: {notebook_name}")
        else:
            print(f"[WARN] Notebook 삭제 실패: {e}")

def delete_endpoint_resources(sm, name):
    """엔드포인트, 설정, 모델 삭제"""
    # 엔드포인트
    endpoint_name = f"{name}-ep"
    try:
        sm.delete_endpoint(EndpointName=endpoint_name)
        print(f"[INFO] Endpoint 삭제 요청: {endpoint_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ValidationException":
            print(f"[WARN] Endpoint 삭제 실패: {e}")

    # EndpointConfig (여러 개 있을 수 있으므로 prefix로 조회)
    try:
        cfgs = sm.list_endpoint_configs(NameContains=name)["EndpointConfigs"]
        for cfg in cfgs:
            cfg_name = cfg["EndpointConfigName"]
            try:
                sm.delete_endpoint_config(EndpointConfigName=cfg_name)
                print(f"[INFO] EndpointConfig 삭제 요청: {cfg_name}")
            except ClientError as e:
                print(f"[WARN] EndpointConfig 삭제 실패({cfg_name}): {e}")
    except ClientError:
        pass

    # 모델 (여러 개 있을 수 있으므로 prefix로 조회)
    try:
        models = sm.list_models(NameContains=name)["Models"]
        for m in models:
            model_name = m["ModelName"]
            try:
                sm.delete_model(ModelName=model_name)
                print(f"[INFO] Model 삭제 요청: {model_name}")
            except ClientError as e:
                print(f"[WARN] Model 삭제 실패({model_name}): {e}")
    except ClientError:
        pass

def cmd_up(args):
    infra = load_infra_json()
    session = boto3.Session(region_name=args.region)
    iam = session.client("iam")
    sm = session.client("sagemaker")

    print("[1/3] SageMaker Execution Role 확인/생성 중...")
    role_arn = ensure_sagemaker_role(iam, args.name, infra)
    print(f"    Role ARN: {role_arn}")

    print("[2/3] VPC/Subnet/SG 확인 중...")
    subnet_id, sg_id = get_default_subnet_and_sg(infra)
    print(f"    SubnetId={subnet_id}, SG={sg_id}")

    print("[3/3] Notebook 생성 중...")
    nb_name = create_sagemaker_notebook(sm, args.name, role_arn, subnet_id, sg_id)
    wait_for_notebook_status(sm, nb_name, "InService", timeout=600)
    print(f"    Notebook 준비 완료: {nb_name}")

    info = {
        "notebook_name": nb_name,
        "role_arn": role_arn,
        "subnet_id": subnet_id,
        "sg_id": sg_id,
        "region": args.region
    }
    with open(SM_INFO_PATH, "w") as f:
        json.dump(info, f, indent=2)
    print("[완료] sagemaker_info.json 저장 완료.")

def cmd_down(args):
    session = boto3.Session(region_name=args.region)
    sm = session.client("sagemaker")

    # Notebook 삭제
    nb_name = f"{args.name}-notebook"
    print("[1/2] Notebook 삭제 시도...")
    delete_sagemaker_notebook(sm, nb_name)

    # Endpoint/Model/Config 삭제
    print("[2/2] Endpoint/Config/Model 삭제 시도...")
    delete_endpoint_resources(sm, args.name)

    print("[완료] SageMaker 관련 리소스 정리 완료.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="Create SageMaker Notebook Instance")
    up.add_argument("--name", required=True)
    up.add_argument("--region", required=True)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="Delete SageMaker Notebook + Endpoint/Model")
    down.add_argument("--name", required=True)
    down.add_argument("--region", required=True)
    down.set_defaults(func=cmd_down)

    args = ap.parse_args()
    args.func(args)
