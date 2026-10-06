# Infrastructure for the onboarding service

This is the project's **own** Terraform: separate state, separate lifecycle. It reads P3's platform (the EKS cluster, its
OIDC providers, the shared ALB, the Route 53 zone) through data sources and **never modifies it**. `terraform destroy` here
removes only what this project created; P3 keeps running. (The reverse is not true: if P3's platform stack is destroyed, the
cluster under us goes with it.)

Status: written and validated (`terraform validate`, `terraform fmt`) but **not applied**. Applying creates AWS resources and
needs your credentials, so it is yours to run.

## What it creates

| Stack | Resources |
|---|---|
| `platform/` | ECR repository `client-onboarding` (immutable tags, scan on push, lifecycle policy); ACM certificate for `qa-proj4-onboarding.<zone>` and `proj4-onboarding.<zone>` with DNS validation records in the existing zone |
| `envs/qa/`, `envs/prod/` (module `onboarding-env`) | Namespace `onboarding-qa` / `onboarding-prod` with quota and limit range; three Secrets Manager secret shells (`<env>/onboarding/pg-secret`, `app-secret`, `langfuse-keys`); External Secrets IAM role and `eso` service account; Bedrock IAM role and `onboarding-api` service account (IRSA, only the listed inference profile and its underlying models); GitHub Actions deploy role (qa: `ref:refs/heads/qa`, prod: `environment:prod`) with an EKS access entry scoped to the namespace; optional Route 53 alias to the shared ALB |

What it reads and does not touch: the cluster `devsecops-eks`, its OIDC provider and the GitHub OIDC provider, the Route 53 hosted
zone, the External Secrets and AWS Load Balancer controllers, the `ebs-sc` and `ebs-sc-retain` StorageClasses.

## Prerequisites

- P3's platform, add-ons and state bucket applied (its `bootstrap` stack creates the S3 bucket used here, with a different key).
- Terraform 1.10 or later, the AWS CLI on `PATH` (the Kubernetes provider fetches tokens with it), credentials that can create the
  resources above, `kubectl` for checks.
- Free capacity on the two nodes. Measure first: `kubectl describe nodes | grep -A8 "Allocated resources"`. This project asks for about
  400m CPU and 800Mi memory per environment (requests).

## Apply, in order

```bash
cd infra/terraform/platform
cp backend.hcl.example backend.hcl            # fill in the state bucket
terraform init -backend-config=backend.hcl && terraform plan && terraform apply

cd ../envs/qa
cp backend.hcl.example backend.hcl && cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=backend.hcl && terraform plan && terraform apply

cd ../prod                                      # same, when you are ready for prod
```

Check the plan before applying: the `platform` and `envs` plans should only add resources. If a plan wants to change or destroy
anything of P3's, stop.

## Set the secret values (once per environment; they are never in Terraform state or Git)

Use letters and digits only for passwords (they are placed in connection URLs). Example for `qa` (repeat with `prod`, fresh values):

```bash
ENV=qa
PW() { openssl rand -hex 24; }
OFFICER1=$(PW); OFFICER2=$(PW); SUBMITTER=$(PW); SMOKE=$(PW)
H() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }

aws secretsmanager put-secret-value --secret-id $ENV/onboarding/pg-secret --secret-string "$(jq -n \
  --arg su "$(PW)" --arg owner "$(PW)" --arg app "$(PW)" --arg bank "$(PW)" \
  '{POSTGRES_PASSWORD:$su, ONBOARDING_OWNER_PASSWORD:$owner, ONBOARDING_APP_PASSWORD:$app, MOCKBANK_PASSWORD:$bank}')"

aws secretsmanager put-secret-value --secret-id $ENV/onboarding/app-secret --secret-string "$(jq -n \
  --arg tokens "[{\"id\":\"officer-1\",\"role\":\"officer\",\"sha256\":\"$(H $OFFICER1)\"},{\"id\":\"officer-2\",\"role\":\"officer\",\"sha256\":\"$(H $OFFICER2)\"},{\"id\":\"submitter-1\",\"role\":\"submitter\",\"sha256\":\"$(H $SUBMITTER)\"},{\"id\":\"smoke-bot\",\"role\":\"submitter\",\"sha256\":\"$(H $SMOKE)\"}]" \
  --arg session "$(PW)" --arg smoke "$SMOKE" \
  '{ONBOARDING_TOKENS:$tokens, SESSION_SECRET:$session, SMOKE_TOKEN:$smoke}')"

# Langfuse keys, or empty strings to leave tracing off for now
aws secretsmanager put-secret-value --secret-id $ENV/onboarding/langfuse-keys \
  --secret-string '{"LANGFUSE_PUBLIC_KEY":"","LANGFUSE_SECRET_KEY":""}'
```

Keep `$OFFICER1`, `$OFFICER2` and `$SUBMITTER` somewhere safe: they are the tokens people sign in with, and only their hashes are stored.
`KYC_API_KEY` goes into `app-secret` too once P3's KYC service has its API key enabled (P3 notes it is off for now).

## GitHub settings (not in Terraform)

- Repository variables: `AWS_ROLE_TO_ASSUME_QA` and `AWS_ROLE_TO_ASSUME_PROD` (the `deploy_role_arn` outputs of the two env stacks).
- A GitHub **Environment named `prod`** with required reviewers: the prod role only trusts jobs that run in it.
- Optional: repository variable `SONAR_ENABLED=true` and secret `SONAR_TOKEN` after creating the SonarCloud project (see `sonar-project.properties`).
- Branch rules as in the root `README.md` (`qa`: block force-push and deletion only, because the pipeline pushes the deployed tag back).
- If a role cannot be assumed, read the real `sub` claim from a workflow run: GitHub's subject uses immutable IDs
  (`repo:meetbrij@<ownerId>/client-onboarding-ai-agent@<repoId>:...`). The default in `variables.tf` was read from the public GitHub API.

## DNS

Applying `platform/` validates the certificate. The hostnames need an alias to P3's shared ALB. After the first deploy has created the
Ingress, set `create_dns_record = true` in the env stack's `terraform.tfvars` and apply again (it finds the ALB by its
`ingress.k8s.aws/stack = devsecops-shared` tag), or create the alias by hand as P3 does.

## Destroy

`envs/prod`, then `envs/qa`, then `platform`. The Kubernetes objects the pipeline applied live inside the namespaces, which Terraform deletes.
The prod Postgres volume uses the `Retain` StorageClass: the EBS volume survives the namespace on purpose; delete it by hand if you really want
it gone. Secrets Manager secrets in prod stay recoverable for 7 days.

## Not covered here

Nothing in this stack changes P3's quotas, nodes or ACM certificate. If the cluster is too small for both projects, the fix (a larger node
group) is a change to P3's platform stack.
