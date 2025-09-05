provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# 기본 VPC와 서브넷
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ECR 리포지토리
resource "aws_ecr_repository" "app" {
  name         = var.ecr_repo_name
  force_delete = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

# CloudWatch Logs 그룹
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = 14
}

# ECS 클러스터
resource "aws_ecs_cluster" "this" {
  name = var.name
}

# ---- (ML 추가) 모델 버킷 & 앱 Task Role ----

resource "aws_s3_bucket" "ml" {
  bucket        = "devops-lab-${data.aws_caller_identity.current.account_id}-ml"
  force_destroy = true
}

resource "aws_iam_role" "task_app" {
  name = "${var.name}-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "ecs-tasks.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_app_s3_read" {
  name = "${var.name}-task-s3-read"
  role = aws_iam_role.task_app.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["s3:GetObject"],
        Resource = "arn:aws:s3:::${aws_s3_bucket.ml.bucket}/models/*"
      },
      {
        Effect   = "Allow",
        Action   = ["s3:ListBucket"],
        Resource = "arn:aws:s3:::${aws_s3_bucket.ml.bucket}",
        Condition = { StringLike = { "s3:prefix": ["models/*"] } }
      }
    ]
  })
}

# ---- Task Execution Role ----
resource "aws_iam_role" "task_execution" {
  name = "${var.name}-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "ecs-tasks.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_exec_policy" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# 보안 그룹
resource "aws_security_group" "alb" {
  name   = "${var.name}-alb-sg"
  vpc_id = data.aws_vpc.default.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "service" {
  name   = "${var.name}-svc-sg"
  vpc_id = data.aws_vpc.default.id

  ingress {
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ALB
resource "aws_lb" "app" {
  name               = "${var.name}-alb"
  internal           = false
  load_balancer_type = "application"
  subnets            = data.aws_subnets.default.ids
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "app" {
  name        = "${var.name}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id

  health_check {
    path    = "/"
    matcher = "200-399"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ECS Task Definition
resource "aws_ecs_task_definition" "app" {
  family                   = var.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.fargate_cpu
  memory                   = var.fargate_memory

  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.task_app.arn

  container_definitions = jsonencode([
    {
      name      = var.name,
      image     = "${aws_ecr_repository.app.repository_url}:latest",
      essential = true,
      portMappings = [{
        containerPort = var.container_port,
        hostPort      = var.container_port,
        protocol      = "tcp"
      }],
      environment = [
        {
          name  = "MODEL_S3_URI",
          value = "s3://${aws_s3_bucket.ml.bucket}/models/${var.name}/latest/model.pkl"
        },
        { name = "MODEL_VERSION", value = "latest" }
      ],
      logConfiguration = {
        logDriver = "awslogs",
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app.name,
          awslogs-region        = var.region,
          awslogs-stream-prefix = var.name
        }
      }
    }
  ])
}

# ECS Service
resource "aws_ecs_service" "app" {
  name            = var.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = var.name
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http]
}

# SageMaker Training Execution Role (학습 잡이 assume)
resource "aws_iam_role" "sm_training_exec" {
  name = "${var.name}-sm-train-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Service = "sagemaker.amazonaws.com" },
      Action = "sts:AssumeRole"
    }]
  })
}

# 최소 권한: S3(models/*) 읽기/쓰기 + 로그
resource "aws_iam_role_policy" "sm_training_exec_inline" {
  name = "${var.name}-sm-train-inline"
  role = aws_iam_role.sm_training_exec.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect: "Allow",
        Action: ["s3:GetObject","s3:PutObject","s3:DeleteObject"],
        Resource: "arn:aws:s3:::${aws_s3_bucket.ml.bucket}/models/*"
      },
      {
        Effect: "Allow",
        Action: ["s3:ListBucket"],
        Resource: "arn:aws:s3:::${aws_s3_bucket.ml.bucket}"
      },
      {
        Effect: "Allow",
        Action: ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
        Resource: "*"
      }
    ]
  })
}

# (선택) SageMaker Model Registry 그룹(이름만 만들어 둠)
resource "aws_sagemaker_model_package_group" "registry" {
  model_package_group_name = "${var.name}-registry"
  model_package_group_description = "Simple registry for ${var.name}"
}
