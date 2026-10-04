variable "region" {
  description = "AWS region for the production bot."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type. Must be ARM (Graviton) to match the arm64 AMI."
  type        = string
  default     = "t4g.micro"
}

variable "github_repo" {
  description = "GitHub repository (owner/name) that the instance clones and that may deploy."
  type        = string
  default     = "iaora/boss-avail-bot"
}

variable "deploy_branch" {
  description = "Branch that production runs and that GitHub Actions deploys from."
  type        = string
  default     = "main"
}

variable "instance_enabled" {
  description = "Whether the EC2 instance exists. Apply with false first to create the bucket and IAM before the cutover."
  type        = bool
  default     = true
}

variable "env_parameter_name" {
  description = "SSM SecureString holding the production .env contents."
  type        = string
  default     = "/monkey-inc/env"
}
