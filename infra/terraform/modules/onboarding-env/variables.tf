variable "env_name" {
  description = "Environment name: qa or prod. The namespace is onboarding-<env_name>."
  type        = string
}

variable "cluster_name" {
  description = "Existing EKS cluster (owned by the P3 platform stack). Read through data sources, never modified."
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the cluster's IAM OIDC provider (IRSA)."
  type        = string
}

variable "oidc_provider" {
  description = "The cluster's OIDC issuer without https:// (IRSA trust conditions)."
  type        = string
}

variable "github_oidc_provider_arn" {
  description = "ARN of the account-wide GitHub Actions OIDC provider."
  type        = string
}

variable "github_repository_claim" {
  description = "This repository as GitHub writes it in the OIDC subject: owner@ownerId/name@repoId."
  type        = string
}

variable "github_subject" {
  description = "Subject suffix that may assume the deploy role: 'ref:refs/heads/qa' or 'environment:prod'."
  type        = string
}

variable "ecr_repository_arn" {
  type = string
}

variable "ecr_actions" {
  description = "ECR actions for the deploy role: push (qa) or retag (prod)."
  type        = list(string)
}

variable "secret_recovery_window_days" {
  description = "0 deletes a secret immediately on destroy (lets qa be torn down and recreated)."
  type        = number
  default     = 0
}

variable "bedrock_inference_profile_ids" {
  description = "Bedrock inference profile IDs the API may invoke. Empty disables the Bedrock role."
  type        = list(string)
  default     = []
}

variable "bedrock_model_regions" {
  description = "Regions the inference profiles route to (the India-only 'in.' profiles use ap-south-1 and ap-south-2)."
  type        = list(string)
  default     = ["ap-south-1", "ap-south-2"]
}

variable "quota" {
  description = "ResourceQuota for the namespace."
  type = object({
    requests_cpu    = string
    requests_memory = string
    limits_cpu      = string
    limits_memory   = string
    pods            = string
    pvcs            = string
    storage         = string
  })
  default = {
    requests_cpu    = "1"
    requests_memory = "2Gi"
    limits_cpu      = "3"
    limits_memory   = "4Gi"
    pods            = "12"
    pvcs            = "2"
    storage         = "10Gi"
  }
}

variable "container_defaults" {
  description = "LimitRange defaults for containers that set no requests or limits."
  type = object({
    request_cpu    = string
    request_memory = string
    limit_cpu      = string
    limit_memory   = string
  })
  default = {
    request_cpu    = "50m"
    request_memory = "64Mi"
    limit_cpu      = "500m"
    limit_memory   = "512Mi"
  }
}

variable "dns_zone_id" {
  description = "Route 53 hosted zone for the alias record (an existing zone, read only). Empty skips the record."
  type        = string
  default     = ""
}

variable "hostname" {
  description = "Host this environment serves, for the alias record."
  type        = string
  default     = ""
}

variable "alb_group_name" {
  description = "Ingress group of the shared ALB (the alb.ingress.kubernetes.io/group.name value), used to find the ALB for the alias record."
  type        = string
  default     = "devsecops-shared"
}

variable "create_dns_record" {
  description = "Create the alias record. Needs the shared ALB to exist (it does once any Ingress in the group is applied)."
  type        = bool
  default     = false
}
