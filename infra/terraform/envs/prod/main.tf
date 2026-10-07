provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      ManagedBy   = "terraform"
      Stack       = "onboarding-prod"
      Environment = "prod"
    }
  }
}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

# Kubernetes credentials are fetched on every call (a fixed token expires after 15 minutes). Needs the AWS CLI on PATH.
provider "kubernetes" {
  host                   = data.aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", var.region]
  }
}

# The cluster's IRSA provider and the account-wide GitHub OIDC provider already exist (P3 created them).
data "aws_iam_openid_connect_provider" "cluster" {
  url = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_ecr_repository" "app" {
  name = var.ecr_repository_name
}

data "aws_route53_zone" "this" {
  name         = var.hosted_zone_name
  private_zone = false
}

module "onboarding_env" {
  source = "../../modules/onboarding-env"

  env_name                 = "prod"
  cluster_name             = var.cluster_name
  oidc_provider_arn        = data.aws_iam_openid_connect_provider.cluster.arn
  oidc_provider            = trimprefix(data.aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://")
  github_oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  github_repository_claim  = var.github_repository_claim

  # Jobs that use a GitHub Environment present an environment subject, so the prod role cannot be assumed from a branch push alone.
  github_subject = "environment:prod"

  ecr_repository_arn = data.aws_ecr_repository.app.arn
  ecr_actions        = ["ecr:BatchGetImage", "ecr:PutImage"]

  bedrock_inference_profile_ids = var.bedrock_inference_profile_ids
  documents_force_destroy       = false # prod documents are never deleted by a stray destroy
  secret_recovery_window_days   = 7     # a deleted prod secret stays recoverable for a week

  dns_zone_id       = data.aws_route53_zone.this.zone_id
  hostname          = var.hostname
  create_dns_record = var.create_dns_record
}
