output "state_bucket_name" {
  description = "S3 bucket used by the controller Terraform backend."
  value       = aws_s3_bucket.terraform_state.id
}

output "backend_config" {
  description = "Values required by terraform init in ../controller."
  value = {
    bucket       = aws_s3_bucket.terraform_state.id
    key          = "controller/terraform.tfstate"
    region       = var.region
    encrypt      = true
    use_lockfile = true
  }
}
