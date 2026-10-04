terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # State stays local (infra/terraform.tfstate, git-ignored). No secrets go into it: the
  # Discord token lives only in the SSM parameter /monkey-inc/env, created outside Terraform.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "monkey-inc"
    }
  }
}
