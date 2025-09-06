# 사용법:
#   python3 infra/main.py up   --name devops-lab --region ap-northeast-2 --container-port 8080
#   python3 infra/main.py down --name devops-lab --region ap-northeast-2
# 옵션:
#   --no-wait : (up 전용) ECS 서비스 안정화 대기 생략

import argparse, json, time, subprocess, os
import boto3
from botocore.exceptions import ClientError

# ---------------------
# helpers
# ---------------------
def wait_until(fn, desc, timeout=900, interval=10):
    start = time.time()
    while True:
        if fn():
            return
        if time.time() - start > timeout:
            raise TimeoutError(f"Timeout waiting for {desc}")
        time.sleep(interval)

def get_account_id():
    return boto3.client("sts").get_caller_identity()["Account"]

def tag_resources(ec2, ids, name, project):
    if not ids: return
    ec2.create_tags(Resources=ids, Tags=[
        {"Key":"Name","Value":name}, {"Key":"Project","Value":project}
    ])

def ensure_log_group(logs, name, retention_days=14):
    try:
        logs.create_log_group(logGroupName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=name, retentionInDays=retention_days)

def ecr_has_latest(ecr, repo_name):
    try:
        imgs = ecr.describe_images(
            repositoryName=repo_name,
            imageIds=[{"imageTag":"latest"}]
        )
        return len(imgs.get("imageDetails", [])) > 0
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ImageNotFoundException", "RepositoryNotFoundException"):
            return False
        raise

def ensure_s3_bucket(s3, name, region):
    account_id = get_account_id()
    bucket_name = f"{name}-{account_id}-bucket"
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )
        print(f"[INFO] S3 버킷 생성: {bucket_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"]:
            print(f"[INFO] 이미 존재하는 S3 버킷: {bucket_name}")
        else:
            raise
    return bucket_name

def save_infra_json(info, filename="infra_info.json"):
    with open(filename, "w") as f:
        json.dump(info, f, indent=2)
    print(f"[INFO] 인프라 정보가 {filename} 파일로 저장되었습니다.")

def load_infra_json(filename="infra_info.json"):
    if not os.path.exists(filename):
        print(f"[WARN] {filename} 파일이 없습니다. 기본 네이밍으로 삭제 시도합니다.")
        return None
    with open(filename) as f:
        return json.load(f)

def delete_s3_bucket(s3, bucket_name):
    try:
        # 버킷 내 객체 모두 삭제
        resp = s3.list_objects_v2(Bucket=bucket_name)
        objects = resp.get("Contents", [])
        if objects:
            s3.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]}
            )
        # 버킷 삭제
        s3.delete_bucket(Bucket=bucket_name)
        print(f"[INFO] S3 버킷 삭제 완료: {bucket_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchBucket":
            print(f"[INFO] S3 버킷이 이미 없습니다: {bucket_name}")
        else:
            print(f"[WARN] S3 버킷 삭제 실패: {e}")

# ----------------
# VPC & networking
# ----------------
def create_vpc_stack(ec2, name, container_port):
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value":True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value":True})
    tag_resources(ec2, [vpc_id], f"{name}-vpc", name)

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    tag_resources(ec2, [igw_id], f"{name}-igw", name)

    rt_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    tag_resources(ec2, [rt_id], f"{name}-rt", name)

    azs = [z["ZoneName"] for z in ec2.describe_availability_zones()["AvailabilityZones"] if z["State"]=="available"][:2]
    sn_ids = []
    for idx, cidr in enumerate(["10.0.1.0/24","10.0.2.0/24"]):
        sn = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=azs[idx])["Subnet"]
        sn_id = sn["SubnetId"]
        ec2.modify_subnet_attribute(SubnetId=sn_id, MapPublicIpOnLaunch={"Value":True})
        tag_resources(ec2, [sn_id], f"{name}-public-{idx+1}", name)
        ec2.associate_route_table(SubnetId=sn_id, RouteTableId=rt_id)
        sn_ids.append(sn_id)

    alb_sg_id = ec2.create_security_group(
        GroupName=f"{name}-alb-sg", Description="ALB SG", VpcId=vpc_id
    )["GroupId"]
    tag_resources(ec2, [alb_sg_id], f"{name}-alb-sg", name)
    ec2.authorize_security_group_ingress(
        GroupId=alb_sg_id,
        IpPermissions=[{
            "IpProtocol":"tcp","FromPort":80,"ToPort":80,
            "IpRanges":[{"CidrIp":"0.0.0.0/0"}]
        }]
    )
    try:
        ec2.authorize_security_group_egress(
            GroupId=alb_sg_id,
            IpPermissions=[{
                "IpProtocol":"-1","FromPort":0,"ToPort":0,
                "IpRanges":[{"CidrIp":"0.0.0.0/0"}]
            }]
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise

    svc_sg_id = ec2.create_security_group(
        GroupName=f"{name}-svc-sg", Description="Service SG", VpcId=vpc_id
    )["GroupId"]
    tag_resources(ec2, [svc_sg_id], f"{name}-svc-sg", name)
    ec2.authorize_security_group_ingress(
        GroupId=svc_sg_id,
        IpPermissions=[{
            "IpProtocol":"tcp","FromPort":container_port,"ToPort":container_port,
            "UserIdGroupPairs":[{"GroupId":alb_sg_id}]
        }]
    )
    try:
        ec2.authorize_security_group_egress(
            GroupId=svc_sg_id,
            IpPermissions=[{
                "IpProtocol":"-1","FromPort":0,"ToPort":0,
                "IpRanges":[{"CidrIp":"0.0.0.0/0"}]
            }]
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise

    return {
        "vpc_id": vpc_id,
        "subnet_ids": sn_ids,
        "alb_sg_id": alb_sg_id,
        "svc_sg_id": svc_sg_id,
    }

# -----------------
# ALB
# -----------------
def create_alb_stack(elbv2, name, subnets, alb_sg_id, container_port, vpc_id):
    lb = elbv2.create_load_balancer(
        Name=f"{name}-alb",
        Subnets=subnets,
        SecurityGroups=[alb_sg_id],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4",
        Tags=[{"Key":"Name","Value":f"{name}-alb"},{"Key":"Project","Value":name}]
    )["LoadBalancers"][0]
    lb_arn, lb_dns = lb["LoadBalancerArn"], lb["DNSName"]

    tg = elbv2.create_target_group(
        Name=f"{name}-tg",
        Protocol="HTTP",
        Port=container_port,
        VpcId=vpc_id,
        TargetType="ip",
        HealthCheckEnabled=True,
        HealthCheckPath="/",
        Matcher={"HttpCode":"200-399"},
        Tags=[{"Key":"Name","Value":f"{name}-tg"},{"Key":"Project","Value":name}]
    )["TargetGroups"][0]
    tg_arn = tg["TargetGroupArn"]

    listener_arn = elbv2.create_listener(
        LoadBalancerArn=lb_arn, Protocol="HTTP", Port=80,
        DefaultActions=[{"Type":"forward","TargetGroupArn":tg_arn}]
    )["Listeners"][0]["ListenerArn"]

    def _lb_active():
        desc = elbv2.describe_load_balancers(LoadBalancerArns=[lb_arn])["LoadBalancers"][0]
        return desc["State"]["Code"] == "active"
    wait_until(_lb_active, "ALB active", timeout=600, interval=10)

    return {"lb_arn": lb_arn, "lb_dns": lb_dns, "tg_arn": tg_arn, "listener_arn": listener_arn}

# --------------
# IAM (ECS Task Execution)
# --------------
def ensure_iam(iam, name):
    exec_role = f"{name}-task-execution"
    trust = {
        "Version":"2012-10-17",
        "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }
    try:
        iam.get_role(RoleName=exec_role)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            iam.create_role(RoleName=exec_role, AssumeRolePolicyDocument=json.dumps(trust))
        else:
            raise
    try:
        iam.attach_role_policy(
            RoleName=exec_role,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        )
    except ClientError:
        pass
    return iam.get_role(RoleName=exec_role)["Role"]["Arn"]

# ----------------------
# ECR / ECS helpers
# ----------------------
def ensure_ecr_repo(ecr, name):
    try:
        return ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    return ecr.create_repository(repositoryName=name)["repository"]

def ensure_ecs_cluster(ecs, name):
    desc = ecs.describe_clusters(clusters=[name])
    arns = [c["clusterArn"] for c in desc.get("clusters", []) if c.get("status")=="ACTIVE"]
    if arns:
        return name
    ecs.create_cluster(clusterName=name)
    return name

def register_task_def(ecs, name, region, account_id, log_group, container_port,
                      image=None, cpu="256", mem="512", exec_role_arn=None):
    if not image:
        image = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{name}:latest"
    cd = {
        "name": name,
        "image": image,
        "essential": True,
        "portMappings":[{"containerPort":container_port,"hostPort":container_port,"protocol":"tcp"}],
        "logConfiguration":{
            "logDriver":"awslogs",
            "options":{
                "awslogs-group": log_group,
                "awslogs-region": region,
                "awslogs-stream-prefix": name
            }
        }
    }
    resp = ecs.register_task_definition(
        family=name,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu=cpu,
        memory=mem,
        executionRoleArn=exec_role_arn,
        containerDefinitions=[cd],
        runtimePlatform={"cpuArchitecture":"X86_64","operatingSystemFamily":"LINUX"}
    )
    return resp["taskDefinition"]["taskDefinitionArn"]

def create_or_update_service(ecs, name, cluster, task_def_arn, subnets, svc_sg_id, tg_arn, container_port, wait=True):
    exists = False
    resp = ecs.describe_services(cluster=cluster, services=[name])
    if resp.get("services"):
        s = resp["services"][0]
        if s.get("status") != "INACTIVE":
            exists = True

    if not exists:
        ecs.create_service(
            cluster=cluster, serviceName=name, taskDefinition=task_def_arn,
            desiredCount=1, launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration":{
                    "subnets": subnets,
                    "securityGroups":[svc_sg_id],
                    "assignPublicIp":"ENABLED"
                }
            },
            loadBalancers=[{
                "targetGroupArn": tg_arn,
                "containerName": name,
                "containerPort": container_port
            }],
            deploymentController={"type":"ECS"}
        )
    else:
        ecs.update_service(cluster=cluster, service=name, taskDefinition=task_def_arn,
                           desiredCount=1, forceNewDeployment=True)

    if not wait:
        return

    def _stable():
        d = ecs.describe_services(cluster=cluster, services=[name])["services"][0]
        return d.get("runningCount", 0) >= 1 and d.get("desiredCount", 1) == d.get("runningCount", 0)
    wait_until(_stable, "ECS service stable", timeout=900, interval=10)

def push_image_to_ecr(repo_uri, region, image_name):
    print("[ECR] 도커 로그인 중...", flush=True)
    login_cmd = [
        "aws", "ecr", "get-login-password", "--region", region
    ]
    password = subprocess.check_output(login_cmd).decode().strip()
    registry = repo_uri.split('/')[0]
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password.encode(), check=True
    )

    print(f"[ECR] 도커 이미지 태깅 중...", flush=True)
    tag_cmd = [
        "docker", "tag", image_name, f"{repo_uri}:latest"
    ]
    subprocess.run(tag_cmd, check=True)

    print(f"[ECR] 도커 이미지 푸시 중...", flush=True)
    push_cmd = [
        "docker", "push", f"{repo_uri}:latest"
    ]
    subprocess.run(push_cmd, check=True)
    print(f"[ECR] 이미지 푸시 완료: {repo_uri}:latest", flush=True)

# -------------------------
# 리소스 삭제 확인 helpers
# -------------------------
def check_deleted_ecs_cluster(ecs, name):
    resp = ecs.list_clusters()
    for arn in resp.get("clusterArns", []):
        if arn.endswith(f"/{name}"):
            return False
    return True

def check_deleted_ecr_repo(ecr, name):
    try:
        ecr.describe_repositories(repositoryNames=[name])
        return False
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryNotFoundException":
            return True
        raise

def check_deleted_log_group(logs, name):
    resp = logs.describe_log_groups(logGroupNamePrefix=name)
    for lg in resp.get("logGroups", []):
        if lg.get("logGroupName") == name:
            return False
    return True

def check_deleted_iam_role(iam, name):
    role = f"{name}-task-execution"
    try:
        iam.get_role(RoleName=role)
        return False
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            return True
        raise

def check_deleted_vpc(ec2, name):
    vpcs = ec2.describe_vpcs()["Vpcs"]
    for v in vpcs:
        tags = {t["Key"]:t["Value"] for t in v.get("Tags", [])}
        if tags.get("Name") == f"{name}-vpc":
            return False
    return True

# -------------------------
# DOWN (best-effort cleanup)
# -------------------------
def cleanup_ecs_elb(ecs, elbv2, cluster, name):
    try:
        ecs.update_service(cluster=cluster, service=name, desiredCount=0)
        def _zero():
            s = ecs.describe_services(cluster=cluster, services=[name])["services"][0]
            return s.get("runningCount", 0) == 0
        wait_until(_zero, "service drain", timeout=600, interval=10)
    except Exception:
        pass
    try:
        ecs.delete_service(cluster=cluster, service=name, force=True)
    except ClientError:
        pass

    try:
        lbs = elbv2.describe_load_balancers()["LoadBalancers"]
        for lb in lbs:
            if lb["LoadBalancerName"] == f"{name}-alb":
                lb_arn = lb["LoadBalancerArn"]
                try:
                    for l in elbv2.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]:
                        try:
                            elbv2.delete_listener(ListenerArn=l["ListenerArn"])
                        except ClientError:
                            pass
                except ClientError:
                    pass
                try:
                    elbv2.delete_load_balancer(LoadBalancerArn=lb_arn)
                except ClientError:
                    pass
    except ClientError:
        pass

    try:
        tgs = elbv2.describe_target_groups()["TargetGroups"]
        for tg in tgs:
            if tg["TargetGroupName"] == f"{name}-tg":
                try:
                    elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                except ClientError:
                    pass
    except ClientError:
        pass

def deregister_task_defs(ecs, family):
    paginator = ecs.get_paginator("list_task_definitions")
    for page in paginator.paginate(familyPrefix=family, status="ACTIVE"):
        for arn in page.get("taskDefinitionArns", []):
            try:
                ecs.deregister_task_definition(taskDefinition=arn)
            except ClientError:
                pass

def delete_ecr_repo(ecr, name):
    try:
        ecr.delete_repository(repositoryName=name, force=True)
    except ClientError:
        pass

def delete_log_group(logs, name):
    try:
        logs.delete_log_group(logGroupName=name)
    except ClientError:
        pass

def delete_iam(iam, name):
    role = f"{name}-task-execution"
    try:
        iam.detach_role_policy(RoleName=role,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
    except ClientError:
        pass
    try:
        for pn in iam.list_role_policies(RoleName=role).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=role, PolicyName=pn)
    except ClientError:
        pass
    try:
        iam.delete_role(RoleName=role)
    except ClientError:
        pass

def nuke_vpc(ec2, name):
    # VPC ID 찾기
    vpcs = ec2.describe_vpcs()["Vpcs"]
    vpc_id = None
    for v in vpcs:
        tags = {t["Key"]:t["Value"] for t in v.get("Tags", [])}
        if tags.get("Name") == f"{name}-vpc":
            vpc_id = v["VpcId"]
            break
    if not vpc_id:
        print(f"[INFO] 삭제할 VPC가 없습니다: {name}-vpc")
        return

    # 1. EC2 Network Interfaces 삭제
    enis = ec2.describe_network_interfaces(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["NetworkInterfaces"]
    for eni in enis:
        try:
            ec2.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])
        except ClientError as e:
            print(f"[WARN] ENI 삭제 실패: {e}")

    # 2. NAT 게이트웨이 삭제
    try:
        nat_gws = ec2.describe_nat_gateways(Filter=[{"Name":"vpc-id","Values":[vpc_id]}])["NatGateways"]
        for nat in nat_gws:
            try:
                ec2.delete_nat_gateway(NatGatewayId=nat["NatGatewayId"])
            except ClientError as e:
                print(f"[WARN] NAT GW 삭제 실패: {e}")
    except Exception:
        pass

    # 3. VPC 엔드포인트 삭제
    try:
        endpoints = ec2.describe_vpc_endpoints(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["VpcEndpoints"]
        for ep in endpoints:
            try:
                ec2.delete_vpc_endpoints(VpcEndpointIds=[ep["VpcEndpointId"]])
            except ClientError as e:
                print(f"[WARN] VPC 엔드포인트 삭제 실패: {e}")
    except Exception:
        pass

    # 4. IGW detach & 삭제
    igws = ec2.describe_internet_gateways()["InternetGateways"]
    for igw in igws:
        tags = {t["Key"]:t["Value"] for t in igw.get("Tags", [])}
        if igw.get("Attachments"):
            for att in igw.get("Attachments", []):
                if att.get("VpcId") == vpc_id:
                    try:
                        ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
                    except ClientError as e:
                        print(f"[WARN] IGW detach 실패: {e}")
        if tags.get("Name") == f"{name}-igw":
            try:
                ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
            except ClientError as e:
                print(f"[WARN] IGW 삭제 실패: {e}")

    # 5. 라우트 테이블 삭제
    for rt in ec2.describe_route_tables(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["RouteTables"]:
        tags = {t["Key"]:t["Value"] for t in rt.get("Tags", [])}
        for assoc in rt.get("Associations", []):
            if not assoc.get("Main", False):
                try:
                    ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
                except ClientError as e:
                    print(f"[WARN] 라우트 테이블 디스어소시에이트 실패: {e}")
        for r in rt.get("Routes", []):
            if r.get("DestinationCidrBlock") == "0.0.0.0/0" and "GatewayId" in r:
                try:
                    ec2.delete_route(RouteTableId=rt["RouteTableId"], DestinationCidrBlock="0.0.0.0/0")
                except ClientError as e:
                    print(f"[WARN] 라우트 삭제 실패: {e}")
        try:
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
        except ClientError as e:
            print(f"[WARN] 라우트 테이블 삭제 실패: {e}")

    # 6. 서브넷 삭제
    for sn in ec2.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]:
        try:
            ec2.delete_subnet(SubnetId=sn["SubnetId"])
        except ClientError as e:
            print(f"[WARN] 서브넷 삭제 실패: {e}")

    # 7. 보안그룹 삭제
    for sg in ec2.describe_security_groups(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["SecurityGroups"]:
        if sg["GroupName"] == "default": continue
        try:
            ec2.delete_security_group(GroupId=sg["GroupId"])
        except ClientError as e:
            print(f"[WARN] 보안그룹 삭제 실패: {e}")
            
    # 8. VPC 삭제
    try:
        ec2.delete_vpc(VpcId=vpc_id)
        print(f"[INFO] VPC 삭제 완료: {vpc_id}")
    except ClientError as e:
        print(f"[WARN] VPC 삭제 실패: {e}")



def save_infra_json(info, filename="infra_info.json"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    print(f"[INFO] 인프라 정보가 {filename} 파일로 저장되었습니다.")

# -------------------------
# commands
# -------------------------
def cmd_up(args):
    session = boto3.Session(region_name=args.region)
    ec2   = session.client("ec2")
    elbv2 = session.client("elbv2")
    ecr   = session.client("ecr")
    logs  = session.client("logs")
    iam   = session.client("iam")
    ecs   = session.client("ecs")
    s3    = session.client("s3")

    name = args.name
    acct = get_account_id()

    print("[1/8] S3 버킷 생성 중...", flush=True)
    bucket_name = ensure_s3_bucket(s3, name, args.region)

    print("[2/8] VPC 및 네트워크 생성 중...", flush=True)
    net = create_vpc_stack(ec2, name, args.container_port)
    print(f"    VPC 생성 완료: {net['vpc_id']}", flush=True)

    print("[3/8] ALB(Application Load Balancer) 생성 중...", flush=True)
    alb = create_alb_stack(elbv2, name, net["subnet_ids"], net["alb_sg_id"], args.container_port, net["vpc_id"])
    print(f"    ALB 생성 완료: {alb['lb_dns']}", flush=True)

    print("[4/8] ECR(Elastic Container Registry) 저장소 확인/생성 중...", flush=True)
    repo = ensure_ecr_repo(ecr, name)
    print(f"    ECR 저장소: {repo['repositoryUri']}", flush=True)

    local_image = args.image if args.image else name
    try:
        push_image_to_ecr(repo['repositoryUri'], args.region, local_image)
    except Exception as e:
        print(f"[WARN] 도커 이미지 푸시 실패: {e}", flush=True)

    print("[5/8] CloudWatch 로그 그룹 확인/생성 중...", flush=True)
    lg = f"/ecs/{name}"
    ensure_log_group(logs, lg)
    print(f"    로그 그룹: {lg}", flush=True)

    print("[6/8] IAM 실행 역할 확인/생성 중...", flush=True)
    exec_arn = ensure_iam(iam, name)
    print(f"    IAM 역할 ARN: {exec_arn}", flush=True)

    print("[7/8] ECS 클러스터 및 태스크 정의 등록 중...", flush=True)
    cluster = ensure_ecs_cluster(ecs, name)
    task_def_arn = register_task_def(
        ecs, name, args.region, acct, lg, args.container_port,
        image=args.image, cpu=str(args.fargate_cpu), mem=str(args.fargate_mem),
        exec_role_arn=exec_arn
    )
    print(f"    ECS 클러스터: {cluster}", flush=True)
    print(f"    태스크 정의 ARN: {task_def_arn}", flush=True)

    if not ecr_has_latest(ecr, name):
        print(f"[WARN] ECR '{name}:latest' 이미지가 없습니다. 먼저 이미지를 푸시하세요.", flush=True)

    print("[8/8] ECS 서비스 생성/업데이트 중...", flush=True)
    create_or_update_service(
        ecs, name, cluster, task_def_arn,
        net["subnet_ids"], net["svc_sg_id"], alb["tg_arn"], args.container_port,
        wait=(not args.no_wait)
    )
    print("    ECS 서비스 생성 완료", flush=True)

    infra_info = {
        "s3_bucket": bucket_name,
        "vpc_id": net["vpc_id"],
        "subnet_ids": net["subnet_ids"],
        "alb_dns": alb["lb_dns"],
        "alb_arn": alb["lb_arn"],
        "alb_sg_id": net["alb_sg_id"],
        "svc_sg_id": net["svc_sg_id"],
        "ecr_repo_url": repo["repositoryUri"],
        "ecs_cluster": cluster,
        "ecs_service": name,
        "task_def_arn": task_def_arn,
        "log_group": lg,
        "iam_role_arn": exec_arn
    }
    save_infra_json(infra_info)

    print("\n[완료] 모든 리소스가 준비되고 정보가 infra_info.json에 저장되었습니다.\n", flush=True)
    print(json.dumps(infra_info, indent=2))
    save_infra_json(infra_info)

def cmd_down(args):
    session = boto3.Session(region_name=args.region)
    ec2   = session.client("ec2")
    elbv2 = session.client("elbv2")
    ecr   = session.client("ecr")
    logs  = session.client("logs")
    iam   = session.client("iam")
    ecs   = session.client("ecs")
    s3    = session.client("s3")

    infra = load_infra_json()
    name = args.name
    account_id = get_account_id()

    bucket_name = infra["s3_bucket"] if infra and "s3_bucket" in infra else f"{name}-{account_id}-bucket"
    vpc_id = infra["vpc_id"] if infra and "vpc_id" in infra else None
    subnet_ids = infra["subnet_ids"] if infra and "subnet_ids" in infra else []
    alb_arn = infra["alb_arn"] if infra and "alb_arn" in infra else None
    alb_sg_id = infra["alb_sg_id"] if infra and "alb_sg_id" in infra else None
    svc_sg_id = infra["svc_sg_id"] if infra and "svc_sg_id" in infra else None
    ecr_repo_url = infra["ecr_repo_url"] if infra and "ecr_repo_url" in infra else None
    cluster = infra["ecs_cluster"] if infra and "ecs_cluster" in infra else name
    log_group = infra["log_group"] if infra and "log_group" in infra else f"/ecs/{name}"
    iam_role_arn = infra["iam_role_arn"] if infra and "iam_role_arn" in infra else None

    print("[1/7] ECS 서비스 및 ALB 삭제 중...", flush=True)
    cleanup_ecs_elb(ecs, elbv2, cluster=cluster, name=name)
    print("    ECS/ALB 삭제 완료", flush=True)

    print("[2/7] ECS 태스크 정의 등록 해제 중...", flush=True)
    deregister_task_defs(ecs, family=name)
    print("    태스크 정의 해제 완료", flush=True)

    print("[3/7] ECS 클러스터 삭제 중...", flush=True)
    try:
        ecs.delete_cluster(cluster=cluster)
        for _ in range(10):
            time.sleep(3)
            if check_deleted_ecs_cluster(ecs, cluster):
                print("    ECS 클러스터 삭제 확인: 성공", flush=True)
                break
            else:
                print("    ECS 클러스터 삭제 확인: 대기 중...", flush=True)
        else:
            print("    ECS 클러스터 삭제 확인: 실패", flush=True)
    except ClientError as e:
        print(f"    ECS 클러스터 삭제 실패: {e}", flush=True)

    print("[4/7] ECR 저장소 삭제 중...", flush=True)
    if ecr_repo_url:
        repo_name = ecr_repo_url.split("/")[-1]
    else:
        repo_name = name
    delete_ecr_repo(ecr, repo_name)
    for _ in range(5):
        time.sleep(2)
        if check_deleted_ecr_repo(ecr, repo_name):
            print("    ECR 삭제 확인: 성공", flush=True)
            break
        else:
            print("    ECR 삭제 확인: 대기 중...", flush=True)
    else:
        print("    ECR 삭제 확인: 실패", flush=True)

    print("[5/7] 로그 그룹 및 IAM 역할 삭제 중...", flush=True)
    delete_log_group(logs, log_group)
    for _ in range(5):
        time.sleep(2)
        if check_deleted_log_group(logs, log_group):
            print("    로그 그룹 삭제 확인: 성공", flush=True)
            break
        else:
            print("    로그 그룹 삭제 확인: 대기 중...", flush=True)
    else:
        print("    로그 그룹 삭제 확인: 실패", flush=True)
    delete_iam(iam, name)
    for _ in range(5):
        time.sleep(2)
        if check_deleted_iam_role(iam, name):
            print("    IAM 역할 삭제 확인: 성공", flush=True)
            break
        else:
            print("    IAM 역할 삭제 확인: 대기 중...", flush=True)
    else:
        print("    IAM 역할 삭제 확인: 실패", flush=True)

    print("[6/7] VPC 및 네트워크 리소스 삭제 중...", flush=True)
    nuke_vpc(ec2, name)
    for _ in range(10):
        time.sleep(3)
        if check_deleted_vpc(ec2, name):
            print("    VPC 삭제 확인: 성공", flush=True)
            break
        else:
            print("    VPC 삭제 확인: 대기 중...", flush=True)
    else:
        print("    VPC 삭제 확인: 실패", flush=True)

    print("[7/7] S3 버킷 삭제 중...", flush=True)
    delete_s3_bucket(s3, bucket_name)

    info = {"ok": True, "message": "deleted (best-effort)"}

    print("\n[완료] 모든 리소스 삭제 시도 및 확인이 끝났습니다.\n", flush=True)
    print(json.dumps(info, indent=2))
    save_infra_json(info)

# -------------------------
# entry
# -------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="Create VPC + ALB + ECS(Fargate) + ECR + S3 + CloudWatch Logs + IAM")
    up.add_argument("--name", required=True)
    up.add_argument("--region", required=True)
    up.add_argument("--container-port", type=int, default=8080)
    up.add_argument("--image", help="Override container image URI (defaults to <acct>.dkr.ecr.<region>.amazonaws.com/<name>:latest)")
    up.add_argument("--fargate-cpu", type=int, default=256)
    up.add_argument("--fargate-mem", type=int, default=512)
    up.add_argument("--no-wait", action="store_true", help="Do not wait service to be stable")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="Delete everything (best-effort)")
    down.add_argument("--name", required=True)
    down.add_argument("--region", required=True)
    down.set_defaults(func=cmd_down)

    args = ap.parse_args()
    args.func(args)