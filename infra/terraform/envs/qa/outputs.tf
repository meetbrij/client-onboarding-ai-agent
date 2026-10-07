output "namespace" {
  value = module.onboarding_env.namespace
}

output "secret_names" {
  description = "Set the values of these Secrets Manager secrets by hand (see infra/terraform/README.md)."
  value       = module.onboarding_env.secret_names
}

output "deploy_role_arn" {
  description = "Set as the AWS_ROLE_TO_ASSUME_QA variable in GitHub."
  value       = module.onboarding_env.deploy_role_arn
}

output "eso_role_arn" {
  value = module.onboarding_env.eso_role_arn
}

output "api_role_arn" {
  value = module.onboarding_env.api_role_arn
}
