terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial backend config: supply bucket/region via `-backend-config=backend.hcl`.
  backend "s3" {}
}
