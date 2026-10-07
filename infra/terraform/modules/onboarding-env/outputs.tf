output "namespace" {
  value = kubernetes_namespace_v1.this.metadata[0].name
}

output "secret_names" {
  description = "Secrets Manager secrets whose values must be set by hand."
  value       = [for s in aws_secretsmanager_secret.this : s.name]
}

output "eso_role_arn" {
  value = aws_iam_role.eso.arn
}

output "deploy_role_arn" {
  description = "Set as the AWS_ROLE_TO_ASSUME_QA or AWS_ROLE_TO_ASSUME_PROD variable in GitHub."
  value       = aws_iam_role.deploy.arn
}

output "api_service_account" {
  value = kubernetes_service_account_v1.api.metadata[0].name
}

output "api_role_arn" {
  value = aws_iam_role.api.arn
}

output "documents_bucket" {
  description = "S3 bucket for the original documents (the pipeline derives the same name for DOCUMENT_BUCKET)."
  value       = aws_s3_bucket.documents.bucket
}
