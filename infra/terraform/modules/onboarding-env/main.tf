# Everything one environment of the onboarding service needs on the shared cluster:
# namespace and quota, secret shells and the External Secrets identity, the Bedrock identity,
# the CI deploy role, and (optionally) the DNS alias. The cluster, its add-ons, the shared ALB and
# the hosted zone belong to the P3 platform and are only read here.

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  namespace = "onboarding-${var.env_name}"
  prefix    = "${var.env_name}/onboarding"
  secrets   = toset(["pg-secret", "app-secret", "langfuse-keys"])
}

# ---------------------------------------------------------------- namespace

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = local.namespace

    labels = {
      environment = var.env_name
      project     = "client-onboarding"
      # Baseline is enforced; restricted only warns so tightening can be seen without breaking pods.
      "pod-security.kubernetes.io/enforce" = "baseline"
      "pod-security.kubernetes.io/warn"    = "restricted"
    }
  }
}

resource "kubernetes_resource_quota_v1" "this" {
  metadata {
    name      = "${local.namespace}-quota"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  spec {
    hard = {
      "requests.cpu"           = var.quota.requests_cpu
      "requests.memory"        = var.quota.requests_memory
      "limits.cpu"             = var.quota.limits_cpu
      "limits.memory"          = var.quota.limits_memory
      "pods"                   = var.quota.pods
      "persistentvolumeclaims" = var.quota.pvcs
      "requests.storage"       = var.quota.storage
    }
  }
}

resource "kubernetes_limit_range_v1" "this" {
  metadata {
    name      = "${local.namespace}-defaults"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  spec {
    limit {
      type = "Container"

      default = {
        cpu    = var.container_defaults.limit_cpu
        memory = var.container_defaults.limit_memory
      }

      default_request = {
        cpu    = var.container_defaults.request_cpu
        memory = var.container_defaults.request_memory
      }
    }
  }
}

# ------------------------------------------------- secret shells + ESO identity

# The containers only. Values are set out of band so they never enter Terraform state:
#   pg-secret      Postgres passwords (see infra/terraform/README.md for the keys)
#   app-secret     officer tokens (hashed), session secret, smoke-test token, optional KYC API key
#   langfuse-keys  Langfuse public and secret key (may be empty: tracing is then off)
resource "aws_secretsmanager_secret" "this" {
  for_each = local.secrets

  name                    = "${local.prefix}/${each.key}"
  description             = "Client onboarding (${var.env_name}): ${each.key}. Values are set manually."
  recovery_window_in_days = var.secret_recovery_window_days
}

data "aws_iam_policy_document" "eso_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.namespace}:eso"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eso" {
  name               = "${var.cluster_name}-${local.namespace}-eso"
  assume_role_policy = data.aws_iam_policy_document.eso_assume.json
}

data "aws_iam_policy_document" "eso_read" {
  statement {
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [for s in aws_secretsmanager_secret.this : s.arn]
  }
}

resource "aws_iam_role_policy" "eso" {
  name   = "read-${local.namespace}-secrets"
  role   = aws_iam_role.eso.id
  policy = data.aws_iam_policy_document.eso_read.json
}

resource "kubernetes_service_account_v1" "eso" {
  metadata {
    name      = "eso"
    namespace = kubernetes_namespace_v1.this.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.eso.arn
    }
  }
}

# ------------------------------------------------------------ CI deploy role

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository_claim}:${var.github_subject}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${var.cluster_name}-${local.namespace}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "DescribeCluster"
    actions   = ["eks:DescribeCluster"]
    resources = [data.aws_eks_cluster.this.arn]
  }

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "EcrRepository"
    actions   = var.ecr_actions
    resources = [var.ecr_repository_arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-${local.namespace}"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# Kubernetes access: edit rights inside this environment's namespace only.
resource "aws_eks_access_entry" "deploy" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deploy.arn
  type          = "STANDARD"

  # Maps the role to a Kubernetes group so the Role below can add what the managed Edit policy lacks.
  kubernetes_groups = ["${local.namespace}-deployers"]
}

resource "aws_eks_access_policy_association" "deploy" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.deploy.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = [local.namespace]
  }

  depends_on = [aws_eks_access_entry.deploy]
}

# AmazonEKSEditPolicy does not cover custom resources; the pipeline applies SecretStore and ExternalSecret.
resource "kubernetes_role_v1" "deploy_external_secrets" {
  metadata {
    name      = "deploy-external-secrets"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  rule {
    api_groups = ["external-secrets.io"]
    resources  = ["externalsecrets", "secretstores"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
}

resource "kubernetes_role_binding_v1" "deploy_external_secrets" {
  metadata {
    name      = "deploy-external-secrets"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.deploy_external_secrets.metadata[0].name
  }

  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = "${local.namespace}-deployers"
  }
}

# ---------------------------------------------- API identity for Amazon Bedrock

locals {
  bedrock_enabled = length(var.bedrock_inference_profile_ids) > 0

  bedrock_profile_arns = [
    for id in var.bedrock_inference_profile_ids :
    "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${id}"
  ]

  # A profile fans out to the underlying model in each of its regions; the caller needs permission on each.
  # The 'in.' prefix is not part of the foundation model ID.
  bedrock_model_arns = distinct(flatten([
    for id in var.bedrock_inference_profile_ids : [
      for region in var.bedrock_model_regions :
      "arn:aws:bedrock:${region}::foundation-model/${trimprefix(id, "in.")}"
    ]
  ]))
}

data "aws_iam_policy_document" "api_assume" {
  count = local.bedrock_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:sub"
      values   = ["system:serviceaccount:${local.namespace}:onboarding-api"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  count = local.bedrock_enabled ? 1 : 0

  name               = "${var.cluster_name}-${local.namespace}-api"
  assume_role_policy = data.aws_iam_policy_document.api_assume[0].json
}

data "aws_iam_policy_document" "api_bedrock" {
  count = local.bedrock_enabled ? 1 : 0

  # The Converse API is authorised by bedrock:InvokeModel. Allow only the listed inference profiles...
  statement {
    sid       = "InvokeInferenceProfiles"
    actions   = ["bedrock:InvokeModel"]
    resources = local.bedrock_profile_arns
  }

  # ...and the underlying models, but only when the call arrives through one of those profiles.
  statement {
    sid       = "InvokeModelsOnlyViaProfiles"
    actions   = ["bedrock:InvokeModel"]
    resources = local.bedrock_model_arns

    condition {
      test     = "StringLike"
      variable = "bedrock:InferenceProfileArn"
      values   = local.bedrock_profile_arns
    }
  }
}

resource "aws_iam_role_policy" "api_bedrock" {
  count = local.bedrock_enabled ? 1 : 0

  name   = "invoke-bedrock-${var.env_name}"
  role   = aws_iam_role.api[0].id
  policy = data.aws_iam_policy_document.api_bedrock[0].json
}

# The service account the API pods run as; IRSA exchanges its token for the role above.
resource "kubernetes_service_account_v1" "api" {
  metadata {
    name      = "onboarding-api"
    namespace = kubernetes_namespace_v1.this.metadata[0].name

    annotations = local.bedrock_enabled ? {
      "eks.amazonaws.com/role-arn" = aws_iam_role.api[0].arn
    } : {}
  }
}

# ------------------------------------------------------------------- DNS alias

data "aws_lb" "shared" {
  count = var.create_dns_record ? 1 : 0

  tags = {
    "ingress.k8s.aws/stack" = var.alb_group_name
  }
}

resource "aws_route53_record" "alias" {
  count = var.create_dns_record ? 1 : 0

  zone_id = var.dns_zone_id
  name    = var.hostname
  type    = "A"

  alias {
    name                   = data.aws_lb.shared[0].dns_name
    zone_id                = data.aws_lb.shared[0].zone_id
    evaluate_target_health = true
  }
}
