variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "client-onboarding"
}

variable "ecr_repository_name" {
  description = "One repository for the single image that runs both the API and the mock bank."
  type        = string
  default     = "client-onboarding"
}

variable "hosted_zone_name" {
  description = "Existing public Route 53 hosted zone (shared with P3). Read only: this stack never creates or deletes it."
  type        = string
  default     = "bolarbrijesh.com"
}

variable "qa_hostname" {
  type    = string
  default = "qa-proj4-onboarding.bolarbrijesh.com"
}

variable "prod_hostname" {
  type    = string
  default = "proj4-onboarding.bolarbrijesh.com"
}
