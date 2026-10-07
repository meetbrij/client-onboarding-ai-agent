# Client Onboarding Case Workflow

An agentic onboarding workflow for a bank-style client application: LangGraph, a KYC extraction tool, sanctions screening, deterministic risk rules, a **human approval gate**, and an idempotent call to a mock core-banking API. Every step leaves an entry in a tamper-evident, append-only audit log. It runs on AWS EKS through GitHub Actions with approval-gated promotion.

> **Status (2026-10-07): built, deployed to `qa` and `prod` on EKS through the GitHub Actions pipeline, and used by hand. Not yet evaluated.** Phases 1 to 4 are done: the full workflow, the officer UI, the audit chain, the document store, Terraform, manifests and the QA and prod pipelines (QA runs green on every merge; the first prod run was approved and succeeded once the prod stack existed). **Evals have not been run, so this README contains no performance numbers**, and none will appear until they come from a file committed in `evals/results/`. The blocker is Bedrock quota in the AWS account (zero for Anthropic models today): live extraction, live LLM wording and the evals wait on it, and the cases show "extraction unavailable" until it is raised. Details and the plan are in [docs/PLAN.md](docs/PLAN.md); accepted gaps are in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Intro

Banks onboard clients under tight rules: verify identity documents, screen against sanctions lists, rate the risk, and let a named compliance officer decide, with a record that stands up to audit. This project builds that flow as an agent and shows where an LLM helps and where it must not.

**The LLM never decides.** Sanctions matching, risk scoring, the recommendation and the final decision path are deterministic Python or a human. The LLM may only summarise the case for the officer, explain a recommendation using the rule outputs it was given, draft a request for missing documents (never sent automatically), and optionally annotate a fuzzy sanctions hit as "likely false positive because...". That annotation is advisory and logged, and it never clears a hit.

It is a portfolio project for Forward Deployed AI Engineer roles in the UAE. It reuses the platform of a sibling project, a KYC document service on EKS ("P3", [prod-grade-e2e-devsecops-pipeline](https://github.com/meetbrij/prod-grade-e2e-devsecops-pipeline)), and patterns from a LangGraph research agent ("MIA", [azure-market-intel-agent](https://github.com/meetbrij/azure-market-intel-agent)). What was reused, and from where, is listed in [docs/DECISIONS.md](docs/DECISIONS.md).

## What we will build

Nine steps. The compliance officer is in the loop at step 5. Steps 1 to 7 and 9 are built and deployed; step 8 (evals) is designed and waiting for Bedrock quota.

| # | Step | What happens |
|---|---|---|
| 1 | **Intake** | An application arrives (applicant details plus documents). A case is created; `case_id` is also the LangGraph `thread_id`. |
| 2 | **Extract** | The KYC service from P3 is called over HTTP inside the cluster as a tool. Per-field confidence and review flags go into the case state. |
| 3 | **Screen** | Names and aliases are fuzzy-matched against a vendored UN Consolidated List snapshot. Each hit records the matched entry, list source and snapshot date, algorithm, score, and whether name, DOB and nationality agreed. |
| 4 | **Assess** | Python rules cover jurisdiction, occupation, missing or low-confidence documents and sanctions-hit status. Output: a risk rating plus the rules that fired. If documents are missing, the LLM drafts a request to the client. |
| 5 | **Approve** | The workflow pauses at a LangGraph `interrupt()`. The officer sees facts, hits with reasons, fired rules and a recommendation with an explanation, then chooses approve, reject or request more info. The decision resumes the graph from the checkpoint. "Request more info" loops back to intake when the documents arrive. |
| 6 | **Execute** | On approval, a mock core-banking API creates the customer record. The call is idempotent, with `case_id` as the idempotency key. |
| 7 | **Audit** | Every step, model call (prompt name and version, model id, token counts), tool call, rule result and human decision is appended to a hash-chained audit log. |
| 8 | **Evals** | 12 synthetic cases with expected outcomes score the final recommendation and the trajectory. Traced in Langfuse, with prompts versioned there. *Not run yet: waits for Bedrock quota so the live KYC service works; see [docs/EVALS.md](docs/EVALS.md). The same 12 cases already run offline as a regression test (`python -m onboarding.graph.build --all`), which is not an evaluation of extraction or of the model.* |
| 9 | **Deploy** | To P3's EKS cluster (`qa`, then approval-gated `prod`) using a copy of P3's GitHub Actions pipeline. *Done: both environments run from the pipeline.* |

### Workflow

```mermaid
flowchart TD
    START([Application received]) --> intake
    intake --> extract
    extract -->|KYC unavailable| deg[/"degraded: extraction_unavailable"/]
    deg --> screen
    extract --> screen
    screen --> assess
    assess --> approve{{"approve<br/>LangGraph interrupt<br/>officer decides"}}
    approve -->|approve, guard passes| execute --> DONE([Customer created])
    approve -->|reject| REJ([Rejected])
    approve -->|request more info| await_docs{{"await_docs<br/>second interrupt"}}
    await_docs -->|documents arrive| intake
    await_docs -->|round cap reached| REJ
```

Python guards the approve path: an officer cannot approve while a sanctions hit has no disposition, or while extraction was unavailable. They can always reject or request more info. There is no edge from `assess` to `execute`, and no configuration flag that skips the officer.

## Application

Two services built into **one container image**, run as two Deployments with different commands.

| Piece | Technology |
|---|---|
| Onboarding API and workflow | Python 3.12, FastAPI, LangGraph, pydantic |
| Mock core-banking | A separate small FastAPI service; creates a customer record, idempotent on `Idempotency-Key` (UNIQUE constraint; a different payload under the same key returns 409) |
| State and audit | PostgreSQL: LangGraph checkpoints (own schema, `thread_id = case_id`) and the `audit_log` table |
| Original documents | A private, encrypted S3 bucket per environment (a local folder in development), opened only by officers through the API, every view audited (D-24) |
| LLM | Claude Haiku 4.5 on Amazon Bedrock, accessed with IRSA. Prompts are managed in Langfuse, with a local fallback copy |
| KYC extraction | The existing P3 service, called over HTTP as a tool |
| Officer UI | Server-rendered pages with a strict Content Security Policy (`script-src 'self'`, one small script that only shows a busy state on slow forms); values rendered as text only |

**API** (OpenAPI at `/docs`; bearer tokens, roles `submitter` and `officer`): `POST /cases` (applicant JSON and documents), `GET /cases`, `GET /cases/{id}` (officers get hits, rules and the recommendation; submitters get status only), `POST /cases/{id}/decision` (officer only; carries the `interrupt_id` it answers), `POST /cases/{id}/documents` (for an information round), `GET /cases/{id}/documents/{doc_ref}` (an original document, officer only), `GET /cases/{id}/audit` and `GET /audit/verify` (officer), `GET /healthz`. Officer and submitter pages are under `/ui`. Mock bank: `POST /customers` (needs `Idempotency-Key`), `GET /customers/{id}`, `GET /healthz`.

**Key behaviours**
- **Explainability:** each recommendation lists the rules that fired and every hit's match reasons. The LLM's wording restates those outputs and cannot change them.
- **Degrade, don't fail:** KYC service down sends the case to the officer flagged "extraction unavailable". LLM down means rules and screening still run and the summary uses a template. Langfuse down turns tracing off and prompts come from the repository.
- **Audit log:** append-only in Postgres (a trigger rejects UPDATE, DELETE and TRUNCATE; the app role has INSERT and SELECT only). Each row stores `prev_hash` and `row_hash` (SHA-256 chain). `python -m onboarding.audit verify` proves integrity and names the first broken row.
- **Crash-safe:** a Postgres checkpointer keyed by `case_id` lets a case resume from its last node after a restart, and a resume decision is bound to the interrupt it answers so a stale or duplicate click is dropped.

**Privacy by design**
- Everything is **synthetic**: no real ID numbers, no real people as applicants, every fixture marked `SPECIMEN`. Sanctions-hit test cases use synthetic applicants whose names are deliberate fuzzy variants of public-list entries.
- **Original documents are kept, because an officer must be able to look at them** (and must when extraction fails). They go to a private, encrypted S3 bucket (a local folder in development), never into the case state, the checkpoint, logs, traces, prompts or the audit log, and never under the applicant's file name. Only officers can open them, through the API, never by a public link; **every view is written to the audit log before any byte is returned**, and the SHA-256 recorded at upload is checked on every read. They are deleted with the case's checkpoints after the retention window, with a bucket lifecycle rule as a backstop.
- Extracted field values live in the case checkpoint (needed to screen and to show the officer) and are purged for finished cases on the same retention schedule.
- Logs, traces, LLM prompts and audit payloads carry no DOB, ID number or address; at most `case_id` and synthetic names.
- The sanctions list is a dated snapshot in `data/sanctions/`, never fetched at runtime in production; every screening result records the source and snapshot date.

## Tech Stack

### Application and AI

| Tool | What it is and why it is here |
|---|---|
| Python 3.12, FastAPI | API, officer pages and the mock bank |
| LangGraph | State graph, `interrupt()` approval gate, Postgres checkpointer |
| Amazon Bedrock (Claude Haiku 4.5) | The four advisory LLM roles; keyless access via IRSA |
| rapidfuzz | Fuzzy name scoring (`token_sort_ratio`) plus deterministic DOB and nationality corroboration |
| PostgreSQL 16 | Checkpoints and the hash-chained audit log |
| Langfuse (Cloud) | Trace per case, a span per node, prompt versions with `production` and `staging` labels |
| pytest, ruff, mypy, uv | Tests (graph tested with a fake LLM), lint, types, dependency management |

### Infrastructure and Pipeline Tooling

*Type:* **Infra** = runs or provisions the platform; **Pipeline** = used in CI/CD; **Both** = spans the two. Items marked *(P3)* already exist and are reused unchanged; everything else is created by this repo's own Terraform stack.

| Tool | What it is and the problem it solves here | Type |
|---|---|---|
| GitHub and GitHub Actions | Source, branch protection and the CI/CD workflows, copied and adapted from P3 so they can change without touching P3 | Pipeline |
| GitHub OIDC | Workflows assume AWS roles with short-lived tokens; no static AWS keys. This repo gets its own deploy roles | Pipeline |
| Docker | One immutable image per commit; the same image runs the API and the mock bank | Pipeline |
| Amazon ECR | Private registry in `ap-south-1`; SHA-tagged images and `prod-<sha>` promotions, own repository for this project | Both |
| Gitleaks, Trivy, Checkov, SBOM (CycloneDX), SonarCloud | The scan stages P3's pipeline runs. In P3 only Gitleaks blocks today (`ENFORCE_SCANS` is off); we will say what blocks in our copy when we decide | Pipeline |
| Terraform | Our own stack: ECR, namespaces and quotas, deploy roles, IRSA role for Bedrock, secret shells, ACM certificate, Route 53 records. Own state, so `terraform destroy` leaves P3 intact | Infra |
| Amazon EKS *(P3)* | The existing cluster (`devsecops-eks`, `ap-south-1`), read through data sources, never modified | Infra |
| Kubernetes and kustomize | Namespaces `onboarding-qa` and `onboarding-prod`; Deployments, Services, Ingress, a Postgres StatefulSet; image tag rewritten at deploy time | Infra |
| AWS Load Balancer Controller and ALB *(P3)* | Our Ingress joins P3's shared ALB group with its own hostnames | Infra |
| AWS Route 53 *(P3 zone)* | Subdomains for this project in the existing hosted zone | Infra |
| AWS ACM | Our own DNS-validated certificate for our two hostnames | Infra |
| AWS Secrets Manager, External Secrets Operator *(P3 operator)*, IRSA | Secret values stay out of Git and Terraform state; each environment's role can read only its own secrets | Infra |
| Amazon EBS CSI and StorageClasses *(P3)* | Persistent volume for Postgres | Infra |
| Prometheus, Grafana, Loki, Tempo, Alloy, OpenTelemetry *(P3)* | Metrics, logs and traces already running in the cluster | Infra |
| Bedrock model access | Claude Haiku 4.5 via an inference profile; the profile and region are configuration | Infra |

Deliberately not used: Argo CD, GitLab CI, any new security tooling beyond P3's pipeline.

## Architecture

### Request flow and workloads

One EKS cluster (P3's), one shared ALB, two environment namespaces owned by this project.

```mermaid
flowchart TD
    U([Officer or client system<br/>qa-proj4-onboarding.bolarbrijesh.com<br/>proj4-onboarding.bolarbrijesh.com]) --> R53[Route 53<br/>existing hosted zone]
    R53 --> ALB[Shared AWS ALB<br/>TLS, HTTP to HTTPS redirect, host routing]
    ACM[ACM certificate<br/>this project's hostnames] -. attached .-> ALB

    subgraph EKS[Existing EKS cluster, ap-south-1]
        subgraph QA[onboarding-qa namespace]
            INGQ[Ingress] --> SVCQ[Service: onboarding-api]
            SVCQ --> APIQ[onboarding-api pod]
            APIQ -->|HTTP, Idempotency-Key = case_id| MBQ[onboarding-mock-bank pod]
            APIQ -->|SQL| PGQ[(Postgres StatefulSet<br/>checkpoints, audit_log, mock bank db)]
            MBQ -->|SQL| PGQ
        end
        subgraph PROD[onboarding-prod namespace]
            INGP[Ingress] --> SVCP[Service: onboarding-api]
            SVCP --> APIP[onboarding-api pod]
            APIP -->|HTTP| MBP[onboarding-mock-bank pod]
            APIP -->|SQL| PGP[(Postgres StatefulSet)]
            MBP -->|SQL| PGP
        end
        KYC[P3 KYC service<br/>existing, in its own namespaces]
    end

    ALB --> INGQ
    ALB --> INGP
    APIQ -->|HTTP tool: extract| KYC
    APIP -->|HTTP tool: extract| KYC
    APIQ -. IRSA .-> BR[Amazon Bedrock<br/>Claude Haiku 4.5]
    APIP -. IRSA .-> BR
    APIQ -. IRSA, encrypted .-> S3Q[(S3 documents bucket, qa<br/>private, officer-only reads)]
    APIP -. IRSA, encrypted .-> S3P[(S3 documents bucket, prod)]
    APIQ -. HTTPS .-> LF[Langfuse Cloud<br/>traces + prompts]
    APIP -. HTTPS .-> LF
```

*The KYC service is reached by its cross-namespace service name (`nodejs-service.<env>.svc.cluster.local`). P3 does not enable an API key on it yet; see "Known gaps" below.*

### Secrets management

External Secrets Operator runs once per cluster (P3). Each of our namespaces has its own service account, IAM role and secrets; the `onboarding-qa` role can read only `qa/onboarding/*` and `onboarding-prod` only `prod/onboarding/*`.

```mermaid
flowchart LR
    subgraph AWS[AWS]
        SMQ[(Secrets Manager<br/>qa/onboarding/pg-secret<br/>qa/onboarding/app-secret<br/>qa/onboarding/langfuse-keys)]
        SMP[(Secrets Manager<br/>prod/onboarding/...)]
        IAMQ[IAM role: ESO onboarding-qa<br/>reads qa/onboarding/* only]
        IAMP[IAM role: ESO onboarding-prod<br/>reads prod/onboarding/* only]
        IAMB[IAM role: onboarding-api<br/>bedrock:InvokeModel on the Haiku 4.5 profile only<br/>S3 read, write, delete on its own documents bucket only]
        SMQ --- IAMQ
        SMP --- IAMP
    end
    subgraph K8S[Existing EKS cluster]
        ESO[External Secrets Operator]
        subgraph QA[onboarding-qa]
            SAQ[ServiceAccount eso, IRSA] --> SSQ[SecretStore] --> ESQ[ExternalSecret] --> KSQ[K8s Secrets]
            KSQ -->|env| PODSQ[API, mock bank, Postgres pods]
            SAA[ServiceAccount onboarding-api, IRSA] --> PODSQ
        end
        subgraph PROD[onboarding-prod]
            SAP[ServiceAccount eso, IRSA] --> SSP[SecretStore] --> ESP[ExternalSecret] --> KSP[K8s Secrets]
        end
        ESO --> SSQ
        ESO --> SSP
    end
    IAMQ -. AssumeRoleWithWebIdentity .-> SAQ
    IAMP -. AssumeRoleWithWebIdentity .-> SAP
    IAMB -. AssumeRoleWithWebIdentity .-> SAA
```

Terraform creates the secret shells and IAM roles but never the values, which are set out-of-band so they never reach Terraform state. `scripts/set_secrets.sh` writes them and **refuses to overwrite a secret that already has a value** (replacing `pg-secret` locks the API out of Postgres, so it needs a second explicit flag). Officer tokens are stored hashed. There are no static AWS credentials anywhere.

### Observability

Two complementary views. P3's stack covers platform and service telemetry; Langfuse covers the agent.

```mermaid
flowchart TD
    W[onboarding-api and mock bank pods]
    W -->|JSON logs: case_id only, no PII| ALLOY[Grafana Alloy] --> LOKI[(Loki)] --> G[Grafana]
    W -->|OTLP traces| TEMPO[(Tempo)] --> G
    W -->|/metrics| PROM[(Prometheus)] --> G
    W -->|trace per case, span per node,<br/>generations with prompt name + version| LF[Langfuse Cloud]
    W -->|every step, model call, tool call,<br/>rule result, decision| AUD[(Postgres audit_log<br/>hash chain)]
    LF --> OFF([Engineer: why did the agent do that])
    G --> OPS([On-call: is the service healthy])
    AUD --> CO([Compliance: prove what happened])
```

Prompt name and version appear in both the Langfuse generation and the audit row, so a model call can be traced from either side. *Prometheus scraping needs a ServiceMonitor, which P3's deploy role cannot create yet (noted in P3); until then `/metrics` is exposed but not collected.*

### Audit log

```mermaid
flowchart LR
    N1[node or decision event] --> A[canonical JSON of the row]
    A --> H[row_hash = SHA-256 of prev_hash + row]
    H --> I[(audit_log INSERT<br/>under advisory lock)]
    I --> T{{trigger: reject UPDATE, DELETE, TRUNCATE}}
    I --> V[verify_audit_chain<br/>recompute every hash in order]
```

The chain detects edits to any row after the fact. A database superuser could drop the trigger, and truncating the tail of the log is detectable only if the head hash is recorded elsewhere (we write it to the case trace and a log line when a case ends). See [docs/CONTROLS.md](docs/CONTROLS.md) for the stated limits.

## CI/CD Pipelines

Copied from P3 and adapted, not called as reusable workflows (D-04). Shape:

```mermaid
flowchart TD
    A([Merge to qa]) --> B[Gitleaks]
    B --> C1[Checkov] & C2[Trivy FS]
    C1 & C2 --> D[Lint + tests]
    D --> E[SonarCloud]
    E --> F[Docker build, tag = git SHA]
    F --> G1[Trivy image scan] & G2[SBOM]
    G1 & G2 --> H[Push to ECR via OIDC role]
    H --> I[Deploy to onboarding-qa, wait for rollout]
    I --> J[Commit deployed tag to qa]
    J --> K{{Manual QA, then PR qa to main}}
    K --> L[Merge to main] --> M{{GitHub Environment prod<br/>required reviewer}}
    M --> N[Retag sha to prod-sha in ECR, same digest]
    N --> O[Deploy to onboarding-prod, wait for rollout] --> P([Live])
```

Principles carried over from P3: build once and promote the artifact (retag, never rebuild); immutable SHA-tagged images, never `latest`; separate QA and prod roles that cannot assume each other; approval before prod. Branches: `feature/*` to `qa` to `main`. A deterministic eval gate joins the pipeline once the evals exist (Phase 3b).

**What the pipeline does and does not enforce today.** It runs Gitleaks (blocks), Checkov, Trivy (filesystem and image), an SBOM, lint, types, the full test suite with a Postgres service and the 12-fixture run (lint and tests block the build). Checkov and Trivy are **report-only** (`ENFORCE_SCANS: "false"`, as in P3): a local run found 44 HIGH and 0 CRITICAL findings in the Debian base image and 18 and 7 Checkov findings, listed with reasons in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). SonarCloud is off until `SONAR_ENABLED=true`. After a QA deploy a smoke test submits one synthetic case. The prod run waits for a reviewer on the GitHub Environment `prod`, retags the QA image (same digest) and deploys it.

## Branching strategy

Same model as P3: `main` (production), `qa` (cut from `main`, auto-deployed to QA), and short-lived `feature/*` branches cut from `qa`.

```mermaid
gitGraph
    commit id: "phase 1" tag: "v0.1.0"
    branch qa
    checkout qa
    branch feature/onb-002-graph
    checkout feature/onb-002-graph
    commit id: "feature work"
    checkout qa
    merge feature/onb-002-graph id: "PR into qa: QA pipeline runs"
    checkout main
    merge qa id: "PR into main: prod pipeline runs" tag: "v0.2.0"
```

| Branch | Cut from | Merged into | Triggers after a successful merge |
|---|---|---|---|
| `feature/*`, `bugfix/*` | `qa` | `qa` (PR) | QA pipeline: scans, build, ECR push, deploy to `onboarding-qa` |
| `qa` | `main` | `main` (PR) | Prod pipeline: wait for approval, retag the QA image, deploy to `onboarding-prod` |
| `hotfix/*` | `main` | `main` and `qa` | Prod pipeline, then back-sync to `qa` |

Names are `<type>/<ticket-id>-<short-slug>` in lowercase. Nothing is pushed directly to `main` or `qa`. Run `make check` (what CI's lint job runs) before every push. On GitHub, `qa` must not require PRs or status checks (the QA
pipeline commits the deployed image tag back to it, as in P3); block force-push and deletion there. `main` requires a PR (0 approvals while solo) and blocks force-push and deletion.

## Environments and access

`qa` and `prod` are two namespaces on one cluster, to keep cost down: `onboarding-qa` and `onboarding-prod`, with separate quotas, secrets, IAM roles and EKS access entries. The QA deploy role has no access to prod. The cluster is shared with P3, so the control plane and nodes are too; namespace RBAC and IAM scoping are the isolation boundary, as in P3. Both environments are deployed: `qa` at `qa-proj4-onboarding.bolarbrijesh.com` and `prod` at `proj4-onboarding.bolarbrijesh.com` (a DNS alias per host, created by Terraform once the shared ALB exists). Destroying this project's Terraform stack removes our namespaces, roles, secrets, document buckets, ECR repository and DNS records and leaves P3 running. The reverse is not true: if P3's platform stack is destroyed, our cluster goes with it.

## Controls

The design is mapped to the CBUAE *Guidance Note on the Consumer Protection and Responsible Adoption and Use of AI and ML by Licensed Financial Institutions* in [docs/CONTROLS.md](docs/CONTROLS.md): human oversight (no auto-approval path), explainability (rules and match reasons), auditability (hash-chained log), model inventory ([docs/MODEL_INVENTORY.md](docs/MODEL_INVENTORY.md): the LLM roles and the deterministic components are listed; evaluation results and the owner are pending), third-party accountability (Bedrock, Langfuse), audited and officer-only access to original documents, and data protection under the UAE PDPL, including what changes for a production deployment in `me-central-1`.

**Caveat:** the official CBUAE text could not be fetched, so the controls table cites topics, not clause numbers, until the PDF is supplied. This is an engineering mapping, not a compliance attestation.

## Evaluation

Twelve synthetic cases covering a clean approval, a true sanctions hit, a DOB-mismatch false positive, missing and low-confidence documents, high-risk jurisdiction and occupation, multiple issues, and the KYC service and the LLM being unavailable. Scored on recommendation, risk rating, sanctions recall and precision, and trajectory (exact match and required steps present), plus an LLM-judged rubric for missing-document drafts reported separately. Details, rubric and quota handling are in [docs/EVALS.md](docs/EVALS.md).

**Results: none yet.** The eval phase starts when P3's KYC service works end to end (its Bedrock quota is currently zero). With only 12 cases the metrics will be illustrative, not statistical, and extraction quality is not measured until the evals run against the live service.

## Repository Layout

What is in the repository today.

```
CLAUDE.md                   Rules and commands for Claude Code in this repo
docs/                       PLAN, CONTROLS, DECISIONS, EVALS, MODEL_INVENTORY, adr/
app/onboarding/             graph/, rules/, screening/, audit/, llm/, tools/, api/ (with the officer UI), documents.py (the document store), service.py, db.py, ...
app/mock_bank/              Mock core-banking FastAPI service (must not import onboarding/)
data/sanctions/             Dated UN list snapshot, manifest (source, date, sha256)
data/reference/             High-risk jurisdictions and occupations, screening config (dated, sourced)
prompts/                    Local fallback copies of the Langfuse prompts
scripts/                    load_sanctions.py, make_fixtures.py, demo_submit.py, smoke_test.py, set_secrets.sh (never overwrites a secret), sync_prompts.py
evals/                      cases/, run.py, metrics.py, judge.py, results/<date>.json
k8s/{base,qa,prod}/         Kustomize: shared manifests (Postgres, API, mock bank, network policies) and the two environment overlays
infra/terraform/            This project's own AWS and cluster resources: platform/, envs/qa, envs/prod, modules/ (see its README for apply steps)
.github/workflows/          qa-cicd.yml and prod-cd.yaml, copied from P3 and adapted
Dockerfile  docker-compose.yml  pyproject.toml  tests/
```

## Local Development

*Works now: everything below, including the full stack in Docker Compose with the officer UI. Not yet: the evals (Phase 3b).*

```bash
make setup                                   # uv sync, pre-commit, copy .env.example to .env
docker compose up -d --build                 # Postgres, mock bank :8001, fake KYC :8002, migrations, API and UI :8000
uv run python scripts/demo_submit.py near_miss_dob_mismatch   # submit a synthetic case, then open http://localhost:8000/ui
uv run pytest                                # offline; add TEST_POSTGRES_ADMIN_URL (see CLAUDE.md) to include the Postgres tests
make check                                   # ruff check, ruff format --check, mypy and the fixture run: exactly what CI's lint job runs
uv run python scripts/load_sanctions.py      # rebuild the screening index from the vendored snapshot
uv run python -m onboarding.graph.build --all   # all 12 fixture cases end to end, offline, compared with their expectations
DATABASE_URL=postgresql+psycopg://onboarding_app:onboarding-app-local@localhost:5432/onboarding \
  uv run python -m onboarding.audit verify   # prove the audit chain in the compose database; non-zero exit on a break
```

Local runs need no AWS: the compose stack uses a fake KYC service and a fake LLM, so the whole workflow can be exercised and the officer pages opened at `http://localhost:8000/ui`. Local development tokens (dev only; qa and prod refuse to start without real ones): `dev-submitter-token` (submitter-1), `dev-officer-token` (officer-1), `dev-officer2-token` (officer-2). Submit as the submitter and decide as an officer: nobody decides a case they submitted. To use real Bedrock, set `AWS_PROFILE`, `AWS_REGION` and `BEDROCK_MODEL_ID` (default: the Haiku 4.5 inference profile, which must be enabled in your account and have quota). To trace, set the Langfuse keys. Use synthetic data only; never put a real person's details into a case.

Settings are environment variables (documented in `.env.example`): database URL, KYC base URL and API key, mock-bank URL, `BEDROCK_MODEL_ID`, `AWS_REGION`, `LLM_ENABLED`, Langfuse host and keys, `OTEL_EXPORTER_OTLP_ENDPOINT`, `MAX_INFO_ROUNDS`.

## Prerequisites

- **To develop:** Docker with Compose, Python 3.12, [uv](https://docs.astral.sh/uv/), `make`.
- **Optional, for live LLM calls:** an AWS account with Amazon Bedrock access to Claude Haiku 4.5 and a non-zero quota (see Known gaps), and a Langfuse Cloud project.
- **To deploy:** P3's platform already applied (EKS cluster, add-ons, observability, Route 53 hosted zone), Terraform 1.10 or later, `kubectl`, AWS CLI, a GitHub repository with Actions, a SonarCloud project, and AWS credentials to apply `infra/terraform` once. After that the pipeline uses OIDC only.

## Known gaps and open dependencies

The full list with reasons is [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). The ones that matter most:

- **Bedrock quota (the critical path):** the AWS account used by P3 has per-minute quota 0 for Anthropic models, so P3's KYC service cannot extract (cases show "extraction unavailable", and approval is blocked until an officer rejects or asks for more information), live LLM wording falls back to templates, and the evals cannot run. The workflow degrades as designed; this is a dependency, not a defect. The cause was inferred from P3's notes and has not been confirmed in the KYC service's logs.
- **No evaluation yet:** no accuracy, recall or trajectory numbers exist. The 12 fixture cases pass as a regression test; that is not an evaluation.
- **Bedrock and Langfuse Cloud were never called for real** (no quota, no project keys). The code is tested with stubs and the real Langfuse SDK against an in-memory exporter.
- **Scans report only** (as in P3); see CI/CD above for what is and is not enforced.
- **CBUAE text:** see Controls. Topics only, no clause numbers, until the PDF is supplied.
- **Matching scope:** a demo-scale fuzzy scorer, not a screening-vendor replacement; a middle name left out can slip under the threshold, and transliterated names can raise false positives.
- **Officer identity:** per-person static tokens stand in for a bank's SSO.
- **Documents are retained** (D-24), encrypted with S3's default key, with no malware scanning or customer-managed key.
- **Audit log:** a database superuser could drop the trigger; see Audit log above.
- **Shared cluster capacity:** two small nodes carry P3, monitoring and both environments of this project.

## Documentation

| Doc | Contents |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | Phases with "done when" checks, resume-claim mapping, cut list, demo script |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every decision, what was reused from P3 and MIA, status |
| [docs/CONTROLS.md](docs/CONTROLS.md) | CBUAE guidance mapping, gaps, PDPL and UAE region notes |
| [docs/EVALS.md](docs/EVALS.md) | The 12 cases, metrics, rubric, quota design |
| [docs/MODEL_INVENTORY.md](docs/MODEL_INVENTORY.md) | The LLM roles, prompts, the deterministic decision components, third-party services |
| [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) | Accepted gaps, kept honest |
| [infra/terraform/README.md](infra/terraform/README.md) | Applying the infrastructure, setting secrets, GitHub settings, recovery |
| [CLAUDE.md](CLAUDE.md) | Hard rules, state schema, commands, pitfalls |

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | README and architecture diagrams | done |
| 1 | Repo scaffolding, sanctions loader, fixtures, mock core-banking | done |
| 2 | Graph to assess, rules, hash-chained audit log | done |
| 3 | Approval interrupt, checkpointer and resume, execute, officer UI, Langfuse | done |
| 4 | Image, manifests, own Terraform stack, pipelines, qa then prod | **done**: both environments deployed through the pipelines |
| 4+ | Fixes and additions found in use: non-blocking API, busy state in the UI, officer access to original documents (D-24), `make check`, safe secret setting | done |
| 3b | Evals against the live KYC service | **blocked on Bedrock quota** |
| 5 | Real eval numbers, controls evidence and screenshots, model inventory, demo clip | not started; the parts that need numbers wait for 3b |

## License

To be decided.
