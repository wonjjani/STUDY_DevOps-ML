flowchart LR
  user[Internet Client] -->|HTTP :80| ALB[ALB\naws_lb.app\nSG: alb-sg]
  ALB --> TG[Target Group\naws_lb_target_group.app\nHealth: GET / → 200-399]
  TG --> SVC[ECS Service\naws_ecs_service.app\nLaunch: Fargate\nDesired: 1]
  SVC --> TASK[Task Definition\naws_ecs_task_definition.app\nContainer: :latest\nPort: 8080]
  TASK -->|pull image| ECR[ECR Repository\naws_ecr_repository.app]
  TASK -->|logs| CW[CloudWatch Logs\naws_cloudwatch_log_group.app\nRetention: 14d]

  subgraph VPC[Default VPC]
    subgraph Subnets[Default Subnets]
      ALB
      SVC
    end
  end

