output "bucket" {
  description = "Private bucket for bot_data/, the one-time DB migration and nightly backups."
  value       = aws_s3_bucket.bot.bucket
}

output "instance_id" {
  description = "Production instance. Shell: aws ssm start-session --target <id>"
  value       = one(aws_instance.bot[*].id)
}

output "deploy_role_arn" {
  description = "Set as the GitHub repository variable AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.github_deploy.arn
}
