output "alb_dns"            { value = aws_lb.app.dns_name }
output "ecr_repo_url"       { value = aws_ecr_repository.app.repository_url }
output "ecs_cluster"        { value = aws_ecs_cluster.this.name }
output "ecs_service"        { value = aws_ecs_service.app.name }

# SageMaker (train)
output "sm_training_exec_role_arn" { value = aws_iam_role.sm_training_exec.arn }

# Model bucket
output "model_bucket"       { value = aws_s3_bucket.ml.bucket }

# (선택) Model Registry 그룹 이름 — 해당 리소스를 만들었을 때만 유지
output "sm_model_package_group" {
  value = aws_sagemaker_model_package_group.registry.model_package_group_name
}
