# Runbook

How to operate the client onboarding service on EKS: check it, deploy it, change its secrets, and recover from the things that have actually gone wrong. Written for the project owner. Everything here is synthetic-data only.

Commands use `qa`; for production replace `qa` with `prod` (namespace `onboarding-prod`). Nothing here prints a secret value; keep it that way. A hook in this repo blocks `get-secret-value` in shell commands on purpose.

## 1. What runs where

| Thing | `qa` | `prod` |
|---|---|---|
| Namespace | `onboarding-qa` | `onboarding-prod` |
| URL | `https://qa-proj4-onboarding.bolarbrijesh.com` (officer UI at `/ui`) | `https://proj4-onboarding.bolarbrijesh.com` |
| Workloads | `onboarding-api` (Deployment), `onboarding-mock-bank` (Deployment), `postgres` (StatefulSet, one pod, EBS volume) | same; the prod volume uses a `Retain` StorageClass |
| Branch and pipeline | `qa`, `qa-cicd.yml` deploys on every push to `qa` | `main`, `prod-cd.yaml`, approval-gated (`prod` GitHub Environment), retags the QA image, no rebuild |
| Secrets (AWS Secrets Manager, `ap-south-1`) | `qa/onboarding/{pg-secret,app-secret,langfuse-keys,kyc-api-key,anthropic-api-key}` | `prod/onboarding/...` |
| Cluster | P3's EKS cluster, shared: its KYC service is `nodejs-service.<env>.svc.cluster.local` in namespaces `qa` and `prod` | same |

What depends on what: the API needs Postgres, the mock bank, P3's KYC service (with `kyc-api-key`), and the Anthropic API (with `anthropic-api-key`). If KYC or the LLM is down, cases still reach the officer ("degrade, don't fail"): extraction shows unavailable, summaries use templates.

## 2. Is it healthy?

```bash
kubectl get pods,externalsecret -n onboarding-qa          # all pods 1/1 Running; every ExternalSecret SecretSynced, READY True
curl -s -o /dev/null -w "%{http_code}\n" https://qa-proj4-onboarding.bolarbrijesh.com/healthz     # 200
kubectl logs -n onboarding-qa deploy/onboarding-api -c api --tail=50
```

End to end: sign in at `/ui` as the submitter, submit a case, sign in as an officer and open it. A healthy case shows extracted fields (not "extraction unavailable") and an LLM-written summary. The pipeline also runs a smoke test after each QA deploy (`scripts/smoke_test.py`, a submitter-only token); it expects extraction to be reported unavailable for its text documents, and the case must still reach the officer.

Verify the audit chain for the whole log (read-only, uses the pod's restricted role):

```bash
kubectl exec -n onboarding-qa deploy/onboarding-api -c api -- python -m onboarding.audit verify
```

Exit 0 means the chain is intact. On a break it exits 1 and names the first broken row: stop, do not delete anything, and investigate (section 8).

## 3. Sign-in tokens

Officers and the submitter sign in with bearer tokens. Only their SHA-256 hashes are stored in `app-secret` (`ONBOARDING_TOKENS`); the plain tokens were printed once by `scripts/set_secrets.sh` when the secret was first written. If a token is lost, it cannot be recovered: replace `app-secret` (section 5) and save the new tokens. That signs out everyone, because the session secret changes too. The same person cannot approve a case they submitted.

## 4. Deploying

- **QA:** open a PR from a `feature/*` branch into `qa`; run `make check` first. After the merge succeeds the QA pipeline builds, scans, pushes to ECR and deploys. The pipeline commits the deployed image tag back to `qa` ("ci: record deployed qa image", `[skip ci]`).
- **Prod:** PR `qa` into `main`. After the merge the prod pipeline waits for approval in the `prod` GitHub Environment, then retags the QA image and deploys.
- **A new secret or manifest dependency:** Terraform apply and the secret values must exist **before** the merge that needs them, in both environments, otherwise the new API pod waits (`CreateContainerConfigError`) and the pipeline's rollout wait times out. The old pod keeps serving meanwhile. Order: `terraform apply` the env stack, `scripts/set_secrets.sh <env>`, then merge.
- **Roll back a bad deploy:** `kubectl rollout undo deployment/onboarding-api -n onboarding-qa`. The next pipeline run redeploys whatever is on the branch, so also revert the commit.
- **Pause an environment** (to save cost): `kubectl scale deploy/onboarding-api deploy/onboarding-mock-bank -n onboarding-qa --replicas=0`; scale back to 1 to resume. Leave Postgres running (its disk is zone-bound, see 7.5).
- Scans are report-only (as in P3); only Gitleaks blocks a build.

## 5. Secrets

Secrets Manager holds the values; External Secrets copies them into the cluster hourly. Never write one by hand.

```bash
scripts/set_secrets.sh qa --dry-run     # what it would do
scripts/set_secrets.sh qa               # fills the secrets that are still empty and SKIPS any that already have a value
```

- **Add or rotate the API keys** (P3's KYC key for that environment; an Anthropic key, which is a project-specific key with a spend limit):

  ```bash
  set -a; source .env.eval; set +a        # .env.eval is git-ignored; it holds KYC_API_KEY and ANTHROPIC_API_KEY
  scripts/set_secrets.sh qa --force kyc-api-key --force anthropic-api-key
  kubectl annotate externalsecret kyc-api-key anthropic-api-key -n onboarding-qa force-sync=$(date +%s) --overwrite
  kubectl rollout restart deployment/onboarding-api -n onboarding-qa
  ```

  Use prod's own P3 key for prod. If a key may have leaked, revoke it at the provider first.
- **`pg-secret` is special.** Postgres reads its passwords only when its volume is first created. Replacing the secret afterwards locks the API and mock bank out at the next restart. The script refuses unless you add `--confirm-pg-secret-overwrite`. Do not do it.
- **If a secret was overwritten by mistake:** do not restart or deploy. Follow "If a secret was overwritten by mistake" in `infra/terraform/README.md` (move `AWSCURRENT` back to the previous version, force-sync, then compare fingerprints). It worked on QA; the fallback when no previous version exists is to reset the three database roles from inside the Postgres pod.
- **Switch the LLM back to Bedrock** (when quota exists): change `LLM_BACKEND` to `bedrock` in `k8s/base/api.yaml`, merge through the pipelines. The India-only `in.` inference profile is already configured; never switch to `global.`. The KYC side is P3's `LLM_PROVIDER`.

## 6. Everyday operations

- **A decision lost to a crash:** a claimed decision is stored with the case, and the service finishes it on start (`service.recover()`). Restart the API pod if a case seems stuck in `executing`.
- **A request that times out in the browser** (504 after 60 seconds at the load balancer) while the case keeps processing: refresh the case page.
- **Retention:** checkpoints of finished cases older than `CHECKPOINT_RETENTION_DAYS` (default 30) are purged by `python -m onboarding.db purge`, which needs the owner role; documents have their own retention in the document store (S3 lifecycle). The audit log is never purged.
- **Rebuild the sanctions index** (local only, never in prod): `uv run python scripts/load_sanctions.py` from the dated snapshot in `data/sanctions/`. The list is vendored; it is never fetched at runtime.
- **Logs and traces:** application logs are in `kubectl logs`; P3's Loki, Tempo and Grafana (port-forward only) also collect them. Langfuse is off in the deployed environments.

## 7. Incidents we have actually had

### 7.1 A new pod stuck in `CreateContainerConfigError` ("secret ... not found")
The Secrets Manager value does not exist yet, or External Secrets has not synced. `kubectl describe pod <pod> -n onboarding-qa | tail`, then `kubectl get externalsecret -n onboarding-qa`. A `SecretSyncedError` with "could not get secret data from provider" means the secret has no value or the Terraform shell is missing: apply the env stack, run `scripts/set_secrets.sh <env>`, force-sync (section 5). The pod recovers by itself.

### 7.2 Cases show "extraction unavailable"
Expected when P3's KYC service is down, throttled, or rejects our key. Check the case's audit trail (officer UI, or `GET /cases/<id>/audit`): the `kyc.extract` row records the outcome and the reason, such as `HTTP 401`, never document content. 401 means a missing or wrong `kyc-api-key`; 502 usually means P3's model provider is failing (its quota or key); a timeout means P3's pods are not ready (`kubectl get pods -n qa`). An officer cannot approve while extraction is unavailable, by design; they can reject or ask for more information, and the client re-sends documents.

### 7.3 Summaries are plain templates
The LLM was unreachable, throttled past its retries, or its output failed the deterministic checks (rule ids cited, draft contents); each case records which. Check `anthropic-api-key` and the provider's status and credit. The workflow is unaffected.

### 7.4 The API pod keeps restarting
Look at `kubectl describe pod`. A restart during a request means the liveness probe failed: synchronous work must never run on the event loop (`run_in_threadpool`; guarded by `tests/test_api_responsiveness.py`). A failing init container is the migration (`onboarding.db migrate`, owner role): wrong password, or Postgres not ready. Do not use blocking advisory locks or leave a transaction open in `migrate`; both stall LangGraph's index creation.

### 7.5 Postgres stuck `Pending` after a pause/resume or node replacement
The cluster has two small nodes and schedules by CPU requests. Postgres's EBS volume lives in one availability zone, so it can only run on that zone's node; if that node is full the pod stays Pending and everything behind it fails. Check `kubectl describe pod postgres-0 -n onboarding-qa` (events: "Insufficient cpu") and `kubectl describe nodes | grep -A8 "Allocated resources"`. Free requests: scale down pods that serve nothing (a broken `onboarding-qa` or a stuck prod pod), or lower requests. Options for lasting headroom: lower requests on both projects' pods (measure first), or raise the node group from 2 to 3 nodes (the maximum is 3; costs roughly 55 USD a month per t3a.large, an estimate: check AWS pricing; the new node may land in the other zone and not help). A node replacement or a restart of the 1a pods can recur until then.

### 7.6 Audit chain verification fails
Treat as a security event. Do not edit or delete rows. Record the first broken row id, compare with a database backup or snapshot if one exists, and check who has the owner role or the superuser (the trigger can be dropped by a superuser; the app role has INSERT and SELECT only).

## 8. Where things are

| Need | Look at |
|---|---|
| Hard rules, state schema, "things that will bite you" | `CLAUDE.md` |
| Infra apply order, restore steps, GitHub settings, destroy | `infra/terraform/README.md` |
| What is accepted as not done | `docs/KNOWN_LIMITATIONS.md` |
| Why things are the way they are | `docs/DECISIONS.md` |
| Controls mapping and evidence | `docs/CONTROLS.md`, `docs/MODEL_INVENTORY.md` |
| Evals and how to re-run them | `docs/EVALS.md`; deterministic: `make evals`; live: `python -m evals.run --live --kyc-url <P3 host>` |
| Demo | `docs/DEMO.md` |

## 9. Tear-down

`terraform destroy` in `envs/prod`, then `envs/qa`, then `platform` (our stacks only; P3's cluster is untouched). The prod Postgres volume is retained on purpose and must be deleted by hand if wanted; prod secrets stay recoverable for 7 days.
