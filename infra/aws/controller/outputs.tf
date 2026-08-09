output "controller_instance_id" {
  description = "EC2 instance managed through Systems Manager."
  value       = aws_instance.controller.id
}

output "controller_public_ip" {
  description = "Egress-only public address; the security group has no ingress rules."
  value       = aws_instance.controller.public_ip
}

output "artifact_bucket_name" {
  description = "Private encrypted project artifact bucket."
  value       = aws_s3_bucket.artifacts.id
}

output "moodle_token_secret_arn" {
  description = "Secret container ARN; Terraform never manages the token value."
  value       = aws_secretsmanager_secret.moodle_token.arn
}

output "ssm_start_session_command" {
  description = "Command for an audited shell without SSH or inbound ports."
  value       = "aws ssm start-session --target ${aws_instance.controller.id} --region ${var.region}"
}
