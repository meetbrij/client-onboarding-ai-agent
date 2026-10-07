variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "client-onboarding"
}

variable "cluster_name" {
  description = "The existing EKS cluster from the P3 platform stack. Read only."
  type        = string
  default     = "devsecops-eks"
}

variable "github_repository_claim" {
  description = "This repository as GitHub writes it in the OIDC subject: owner@ownerId/name@repoId. The immutable IDs stop a renamed or recreated repository from inheriting the role. Read the real value from the 'sub' claim of a workflow run if the role cannot be assumed."
  type        = string
  default     = "meetbrij@4499354/client-onboarding-ai-agent@1405937534"
}

variable "ecr_repository_name" {
  type    = string
  default = "client-onboarding"
}

variable "hosted_zone_name" {
  type    = string
  default = "bolarbrijesh.com"
}

variable "hostname" {
  type = string
}

variable "create_dns_record" {
  description = "Create the Route 53 alias to the shared ALB. Turn on after the first deploy, once the Ingress has created the ALB."
  type        = bool
  default     = false
}

variable "bedrock_inference_profile_ids" {
  description = "Inference profiles the API may call. P3 uses the India-only profile (inference stays in ap-south-1 and ap-south-2); never switch to a global. profile silently."
  type        = list(string)
  default     = ["in.anthropic.claude-haiku-4-5-20251001-v1:0"]
}
