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

output "telegram_config_secret_arn" {
  description = "Telegram secret container ARN; Terraform never manages its value."
  value       = aws_secretsmanager_secret.telegram_config.arn
}

output "ssm_start_session_command" {
  description = "Command for an audited shell without SSH or inbound ports."
  value       = "aws ssm start-session --target ${aws_instance.controller.id} --region ${var.region}"
}

output "lab_provisioner_role_arn" {
  description = "Exact role the controller assumes for approved lab lifecycle operations."
  value       = aws_iam_role.lab_provisioner.arn
}

output "lab_subnet_id" {
  description = "Dedicated egress-only subnet for ephemeral labs."
  value       = aws_subnet.lab.id
}

output "lab_security_group_id" {
  description = "No-ingress security group for ephemeral labs."
  value       = aws_security_group.lab.id
}

output "lab_instance_profile_name" {
  description = "Capability-limited instance profile attached to labs."
  value       = aws_iam_instance_profile.lab.name
}

output "lab_windows_ami_parameter" {
  description = "Public SSM parameter used to resolve the approved Windows base image."
  value       = data.aws_ssm_parameter.windows_server_2022_ami.name
}

output "lab_instance_type" {
  description = "Operator-selected instance type enforced by the lab adapter."
  value       = var.lab_instance_type
}

output "lab_root_volume_size_gib" {
  description = "Operator-selected encrypted lab root volume size."
  value       = var.lab_root_volume_size_gib
}
