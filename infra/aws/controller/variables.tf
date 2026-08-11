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

variable "lab_instance_type" {
  description = "Fixed EC2 size for approved ephemeral Windows labs."
  type        = string
  default     = "t3.large"

  validation {
    condition     = contains(["t3.large", "m6i.large"], var.lab_instance_type)
    error_message = "lab_instance_type must be t3.large or m6i.large."
  }
}

variable "lab_root_volume_size_gib" {
  description = "Encrypted root volume size for approved ephemeral labs."
  type        = number
  default     = 80

  validation {
    condition     = var.lab_root_volume_size_gib >= 50 && var.lab_root_volume_size_gib <= 500
    error_message = "lab_root_volume_size_gib must be between 50 and 500 GiB."
  }
}

variable "lab_hard_ttl_seconds" {
  description = "Independent maximum lab lifetime enforced by the EventBridge Lambda reaper."
  type        = number
  default     = 14400

  validation {
    condition     = var.lab_hard_ttl_seconds >= 10800 && var.lab_hard_ttl_seconds <= 86400 && floor(var.lab_hard_ttl_seconds) == var.lab_hard_ttl_seconds
    error_message = "lab_hard_ttl_seconds must be an integer from 10800 (3 hours) through 86400 (24 hours)."
  }
}

variable "lab_reaper_max_terminations_per_run" {
  description = "Maximum stale labs the independent reaper can terminate during one five-minute invocation."
  type        = number
  default     = 20

  validation {
    condition     = var.lab_reaper_max_terminations_per_run >= 1 && var.lab_reaper_max_terminations_per_run <= 20 && floor(var.lab_reaper_max_terminations_per_run) == var.lab_reaper_max_terminations_per_run
    error_message = "lab_reaper_max_terminations_per_run must be an integer from 1 through 20."
  }
}

variable "additional_tags" {
  description = "Additional non-sensitive resource tags."
  type        = map(string)
  default     = {}
}

variable "scheduler_course_shortnames" {
  description = "Explicit Moodle course shortnames for scheduler discovery; empty only with scheduler_all_courses."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for shortname in var.scheduler_course_shortnames :
      length(shortname) > 0 &&
      (length(base64encode(shortname)) / 4 * 3 -
      (endswith(base64encode(shortname), "==") ? 2 : endswith(base64encode(shortname), "=") ? 1 : 0)) <= 255 &&
      !can(regex("[\\x00-\\x1F\\x7F]", shortname))
      ]) && length(distinct(var.scheduler_course_shortnames)) == length(var.scheduler_course_shortnames) && length(var.scheduler_course_shortnames) <= 64 && sum([
      for shortname in var.scheduler_course_shortnames :
      length(base64encode(shortname)) / 4 * 3 -
      (endswith(base64encode(shortname), "==") ? 2 : endswith(base64encode(shortname), "=") ? 1 : 0)
    ]) <= 2048
    error_message = "scheduler_course_shortnames must be exact unique non-empty names without ASCII controls (64 names, 2048 UTF-8 bytes total, 255 per name)."
  }

  validation {
    condition     = (var.scheduler_all_courses && length(var.scheduler_course_shortnames) == 0) || (!var.scheduler_all_courses && length(var.scheduler_course_shortnames) > 0)
    error_message = "Set exactly one scheduler scope: non-empty scheduler_course_shortnames or scheduler_all_courses=true."
  }
}

variable "scheduler_all_courses" {
  description = "Deliberately discover assignments in every course; cannot be combined with scheduler_course_shortnames."
  type        = bool
  default     = false
}

variable "scheduler_max_new_events_per_cycle" {
  description = "Bounded number of new approval events created in one scheduler cycle."
  type        = number
  default     = 4

  validation {
    condition     = var.scheduler_max_new_events_per_cycle >= 1 && var.scheduler_max_new_events_per_cycle <= 100 && floor(var.scheduler_max_new_events_per_cycle) == var.scheduler_max_new_events_per_cycle
    error_message = "scheduler_max_new_events_per_cycle must be an integer from 1 through 100."
  }
}
