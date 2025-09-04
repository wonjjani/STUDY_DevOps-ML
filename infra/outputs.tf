output "alb_dns" { value = aws_lb.app.dns_name }
output "ecr_repo_url" { value = aws_ecr_repository.app.repository_url }
output "ecs_cluster" { value = aws_ecs_cluster.this.name }
output "ecs_service" { value = aws_ecs_service.app.name }
output "model_bucket" { value = aws_s3_bucket.ml.bucket }
