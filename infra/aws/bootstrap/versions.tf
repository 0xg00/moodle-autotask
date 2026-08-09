terraform {
  required_version = ">= 1.15.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.58.0"
    }
  }

  backend "s3" {
    key          = "bootstrap/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
