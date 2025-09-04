variable "name" {
  type    = string
  default = "devops-lab"
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "ecr_repo_name" {
  type    = string
  default = "devops-lab"
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "fargate_cpu" {
  type    = string
  default = "256"
}

variable "fargate_memory" {
  type    = string
  default = "512"
}
