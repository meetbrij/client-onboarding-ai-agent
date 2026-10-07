# Infrastructure for the onboarding service

This is the project's **own** Terraform: separate state, separate lifecycle. It reads P3's platform (the EKS cluster, its
OIDC providers, the shared ALB, the Route 53 zone) through data sources and **never modifies it**. `terraform destroy` here
removes only what this project created; P3 keeps running. (The reverse is not true: if P3's platform stack is destroyed, the
cluster under us goes with it.)

Status: applied by the owner for `platform/`, `envs/qa` and `envs/prod`; the pipelines have deployed both environments. It was validated with `terraform validate` and `terraform fmt` before the first apply. Applying creates AWS resources and
needs your credentials, so it is yours to run.

## What it creates

| Stack | Resources |
|---|---|
| `platform/` | ECR repository `client-onboarding` (immutable tags, scan on push, lifecycle policy); ACM certificate for `qa-proj4-onboarding.<zone>` and `proj4-onboarding.<zone>` with DNS validation records in the existing zone |
| `envs/qa/`, `envs/prod/` (module `onboarding-env`) | S3 bucket `client-onboarding-docs-<account>-<env>-<region>` for the original documents (private, AES-256, TLS-only, lifecycle expiry; the API role can read, write and delete objects in it); Namespace `onboarding-qa` / `onboarding-prod` with quota and limit range; three Secrets Manager secret shells (`<env>/onboarding/pg-secret`, `app-secret`, `langfuse-keys`); External Secrets IAM role and `eso` service account; Bedrock IAM role and `onboarding-api` service account (IRSA, only the listed inference profile and its underlying models); GitHub Actions deploy role (qa: `ref:refs/heads/qa`, prod: `environment:prod`) with an EKS access entry scoped to the namespace; optional Route 53 alias to the shared ALB |

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

Use the script. It fills the secrets that are still empty and **refuses to overwrite one that already has a value**:

```bash
scripts/set_secrets.sh qa --dry-run     # shows what it would do, changes nothing
scripts/set_secrets.sh qa               # writes pg-secret, app-secret and langfuse-keys where they are empty
scripts/set_secrets.sh prod             # the same for prod, with its own fresh values
```

- It prints the sign-in tokens for `officer-1`, `officer-2` and `submitter-1` **once**, when it writes `app-secret`. Save them: only their hashes are stored.
  The smoke-test token is stored in the secret and read by the pipeline; you do not need it.
- A secret that already has a value is skipped and its current version id is shown. To replace one on purpose:
  `scripts/set_secrets.sh qa --force app-secret`. It prints the old and new version ids and the exact roll-back command.
- **`pg-secret` needs a second flag** even with `--force`: `--confirm-pg-secret-overwrite`. Postgres reads its passwords only when its volume is first created, so
  replacing them later locks the API and the mock bank out of the database at the next pod restart or deploy. Do not do it unless the database does not exist yet.
- Langfuse keys: put them in the environment first (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`) to store real ones; otherwise the secret gets empty strings and tracing stays off.
- The script never reads a secret's value (it asks Secrets Manager only whether a current version exists), needs `aws`, `jq` and `openssl`, and is tested against a fake `aws`
  (`tests/test_set_secrets.py`).
- `KYC_API_KEY` goes into `app-secret` too once P3's KYC service has its API key enabled (P3 notes it is off for now); add it with `--force app-secret` and a re-run of the tokens, or by hand.

### If a secret was overwritten by mistake

Secrets Manager keeps the previous value as `AWSPREVIOUS`. Do not restart pods or deploy until it is restored.

```bash
aws secretsmanager list-secret-version-ids --secret-id qa/onboarding/pg-secret --include-deprecated --region ap-south-1 \
  --query 'Versions[].{id:VersionId,stages:VersionStages,created:CreatedDate}' --output table
aws secretsmanager update-secret-version-stage --secret-id qa/onboarding/pg-secret --region ap-south-1 \
  --version-stage AWSCURRENT --move-to-version-id <PREVIOUS_ID> --remove-from-version-id <CURRENT_ID>
kubectl annotate externalsecret pg-secret app-secret -n onboarding-qa force-sync=$(date +%s) --overwrite
```

Then prove the cluster secret equals what the running pods use, by comparing fingerprints (never print the values):

```bash
kubectl get secret pg-secret -n onboarding-qa -o jsonpath='{.data.ONBOARDING_APP_PASSWORD}' | base64 -d | shasum -a 256
kubectl exec -n onboarding-qa deploy/onboarding-api -c api -- sh -c 'printf %s "$ONBOARDING_APP_PASSWORD" | sha256sum'
```

If `AWSPREVIOUS` is not the original (the secret was overwritten more than once), the running Postgres pod still has the original passwords in its environment: rebuild the
secret from `kubectl exec -n onboarding-qa postgres-0 -- env` (the four password variables) and write it with `put-secret-value` from a private file, then repeat the checks.

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

## Updating an existing environment for the document store (D-24)

Re-apply `envs/qa` (and later `envs/prod`) from this branch. The plan should **add** the S3 bucket and its policies and the `documents-<env>` role policy, and should
**move** (not replace) the API IAM role: a `moved` block turns `aws_iam_role.api[0]` into `aws_iam_role.api`. If the plan wants to destroy and recreate the API role, stop and tell me.
The pods get `DOCUMENT_STORE=s3` and the bucket name from the pipeline on the next deploy; until the bucket exists the API refuses to start (qa and prod require `DOCUMENT_STORE=s3`),
so apply Terraform **before** merging the code.
