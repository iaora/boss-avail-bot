data "aws_caller_identity" "current" {}

locals {
  name         = "monkey-inc"
  account_id   = data.aws_caller_identity.current.account_id
  instance_tag = "monkey-inc-prod"
}

# ---------------------------------------------------------------------------
# S3: bot_data/ (roster CSV, squad timings, class icons), migration/ (the one-time
# upload of the local database) and backups/ (nightly copies from the instance).
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "bot" {
  bucket = "${local.name}-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "bot" {
  bucket                  = aws_s3_bucket.bot.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "bot" {
  bucket = aws_s3_bucket.bot.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bot" {
  bucket = aws_s3_bucket.bot.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "bot" {
  bucket = aws_s3_bucket.bot.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# ---------------------------------------------------------------------------
# EC2 instance: Amazon Linux 2023 (arm64), default VPC, no inbound ports.
# Shell access is through SSM Session Manager.
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_vpc" "default" {
  default = true
}

# Only use availability zones that offer the instance type (not every us-east-1 AZ has t4g).
data "aws_ec2_instance_type_offerings" "available" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.available.locations
  }
}

resource "aws_security_group" "bot" {
  name        = "${local.name}-bot"
  description = "Monkey Inc bot: outbound only (Discord, GitHub, AWS APIs)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "bot" {
  count = var.instance_enabled ? 1 : 0

  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.bot.id]
  iam_instance_profile        = aws_iam_instance_profile.bot.name
  associate_public_ip_address = true

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    repo_url  = "https://github.com/${var.github_repo}.git"
    branch    = var.deploy_branch
    bucket    = aws_s3_bucket.bot.bucket
    region    = var.region
    env_param = var.env_parameter_name
  })

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = {
    Name = local.instance_tag
  }

  # The SQLite database lives on this instance. Never replace it just because a newer AMI
  # came out or the bootstrap script changed; code updates arrive through deploy/deploy.sh.
  lifecycle {
    ignore_changes = [ami, user_data, subnet_id]
  }
}

# ---------------------------------------------------------------------------
# Instance role: SSM agent, read the .env parameter, read/write the bucket.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "bot" {
  name = "${local.name}-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.bot.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "bot" {
  name = "${local.name}-instance"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadEnv"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${var.env_parameter_name}"
      },
      {
        Sid      = "DecryptEnv"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" }
        }
      },
      {
        Sid      = "ListBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.bot.arn
      },
      {
        Sid      = "ReadWriteObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.bot.arn}/*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "bot" {
  name = "${local.name}-instance"
  role = aws_iam_role.bot.name
}

# ---------------------------------------------------------------------------
# GitHub Actions: OIDC login (no stored AWS keys) and a role that may only run
# shell commands on the production instance, and only from the deploy branch.
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/${var.deploy_branch}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name}-github-deploy"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunShellScriptDocument"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = "arn:aws:ssm:${var.region}::document/AWS-RunShellScript"
      },
      {
        Sid      = "OnlyTheBotInstance"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = "arn:aws:ec2:${var.region}:${local.account_id}:instance/*"
        Condition = {
          StringEquals = { "ssm:resourceTag/Name" = local.instance_tag }
        }
      },
      {
        Sid      = "FollowCommand"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ec2:DescribeInstances"]
        Resource = "*"
      },
    ]
  })
}
