output "ecr_repository_name" {
  value = aws_ecr_repository.app.name
}

output "ecr_repository_arn" {
  value = aws_ecr_repository.app.arn
}

output "certificate_arn" {
  description = "Informational: the ALB controller discovers the certificate by hostname."
  value       = aws_acm_certificate_validation.this.certificate_arn
}

output "hosted_zone_id" {
  value = data.aws_route53_zone.this.zone_id
}
