variable "region" {
  description = "AWS region that stores Terraform state."
  type        = string
  default     = "eu-south-2"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]$", var.region))
    error_message = "region must be a valid AWS region name."
  }
}

variable "expected_account_id" {
  description = "Twelve-digit AWS account guardrail."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must contain exactly 12 digits."
  }
}

variable "project_name" {
  description = "Stable project name used in resource names and tags."
  type        = string
  default     = "moodle-autotask"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,30}$", var.project_name))
    error_message = "project_name must be 3-31 lowercase alphanumeric or hyphen characters."
  }
}

variable "additional_tags" {
  description = "Additional non-sensitive resource tags."
  type        = map(string)
  default     = {}
}
