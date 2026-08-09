variable "region" {
  description = "Primary AWS region."
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

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "development"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,19}$", var.environment))
    error_message = "environment must be 2-20 lowercase alphanumeric or hyphen characters."
  }
}

variable "controller_instance_type" {
  description = "EC2 size for the continuously running Linux controller."
  type        = string
  default     = "t3.small"

  validation {
    condition     = contains(["t3.small", "t3.medium"], var.controller_instance_type)
    error_message = "controller_instance_type must be t3.small or t3.medium."
  }
}

variable "root_volume_size_gib" {
  description = "Encrypted gp3 root volume size."
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_size_gib >= 20 && var.root_volume_size_gib <= 100
    error_message = "root_volume_size_gib must be between 20 and 100 GiB."
  }
}

variable "additional_tags" {
  description = "Additional non-sensitive resource tags."
  type        = map(string)
  default     = {}
}
