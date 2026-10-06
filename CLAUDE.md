# CLAUDE.md

Guidance for Claude Code in this repository. Status: **Phases 1 and 2 done: sanctions scorer, risk rules, audit chain, intake to assess graph with the approval pause, LLM layer (fakes + Bedrock client), offline CLI. Not yet: resume and execute, API, UI, Langfuse, Postgres checkpointer (Phase 3); evals (3b); deploy (4).**
Build phase by phase as in `docs/PLAN.md`; decisions in `docs/DECISIONS.md` are accepted unless marked otherwise.

## Purpose
An agentic client-onboarding workflow for a bank-style application. A compliance officer stays in the loop and every step
leaves an auditable trail. Portfolio project for Forward Deployed AI Engineer roles in the UAE: it must look like something a
bank could run, and it must stay thin. Plan: `docs/PLAN.md`. Controls: `docs/CONTROLS.md`. Decisions: `docs/DECISIONS.md`.
Evals: `docs/EVALS.md`. It reuses platform and patterns from two sibling repos (P3 = KYC on EKS, MIA = LangGraph agent);
copy patterns, not code, and record what was reused in `docs/DECISIONS.md`.

## Architecture
```mermaid
flowchart LR
  C([Client application]) --> API[onboarding-api FastAPI]
  O([Compliance officer]) --> UI[Officer UI, server-rendered, strict CSP] --> API
  API --> G{{LangGraph<br/>thread_id = case_id}}
  G --> I[intake] --> E[extract] --> S[screen] --> A[assess] --> AP[approve<br/>interrupt]
  AP -->|approve| X[execute] --> END([done])
  AP -->|reject| END
  AP -->|request more info| I
  E -. HTTP tool .-> KYC[P3 KYC service<br/>same cluster]
  S --> SL[(data/sanctions snapshot)]
  X -. Idempotency-Key = case_id .-> CB[mock-core-banking FastAPI]
  G --> PG[(Postgres: checkpoints + audit_log<br/>hash chain, append-only trigger)]
  G -. LLM, advisory only .-> BR[Bedrock Claude Haiku 4.5]
  G -. traces + prompts .-> LF[Langfuse]
```
Every node writes an audit row. LLM calls are only the four listed in Hard rules.

## Repo layout (planned)
```
CLAUDE.md  README.md  docs/{PLAN,CONTROLS,DECISIONS,EVALS,MODEL_INVENTORY}.md  docs/adr/
app/onboarding/        graph/ (state, nodes, build), rules/ (risk rules), screening/, audit/, llm/, tools/ (kyc, bank),
                       api/, ui/ (templates + static)
app/mock_bank/         separate small FastAPI service (same image, different command; must not import onboarding/)
data/sanctions/        dated snapshot + manifest (source, date, sha256); scripts/load_sanctions.py builds the index
data/reference/        high-risk jurisdictions and occupations (dated, sourced)
prompts/               local fallback copies of the Langfuse prompts
evals/                 cases/*.yaml, run.py, metrics.py, judge.py, results/<date>.json
k8s/{qa,prod}/         kustomize, P3 pattern     infra/terraform/   our own stack on P3's cluster (D-03, D-10)
.github/workflows/     copied and adapted from P3
Dockerfile  docker-compose.yml  pyproject.toml  tests/  (incl. API-to-mock-bank contract test)
```

## Commands (targets; they must exist by the end of the phase noted in PLAN.md)
```bash
make setup                                   # uv sync; create .env from .env.example
docker compose up -d --build                 # works now: postgres, mock-bank :8001, fake-kyc :8002 (api joins in Phase 2)
uv run python scripts/load_sanctions.py      # rebuild index from data/sanctions snapshot (never run in prod)
uv run pytest                                # offline; set MOCK_BANK_TEST_DATABASE_URL (compose postgres) to include the Postgres race test
uv run python scripts/make_fixtures.py       # regenerate evals/cases/*.yaml (committed; tests check them against the snapshot)
uv run ruff check . && uv run mypy app tests
# evals are deferred until P3's KYC service is live (D-15, Phase 3b):
uv run python -m evals.run --live            # live KYC + Bedrock + Langfuse; writes evals/results/<date>.json
uv run python -m onboarding.audit verify     # verify_audit_chain against $DATABASE_URL: exit 1 and the first broken row on a break
uv run python -m onboarding.graph.build --all   # offline: all 12 fixtures to the approval pause, compared with their expectations
uv run python -m onboarding.graph.build --case evals/cases/clean_approve.yaml   # one case (scripted human steps arrive in Phase 3)
# Postgres-backed tests: export TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://postgres:postgres-local@localhost:5432/postgres
# deploy: push to qa => QA pipeline; PR qa -> main => prod pipeline, approval-gated retag (see PLAN Phase 4)
```

## State schema (`CaseState`, pydantic; everything here is checkpointed, so keep it small and PII-light)
```
case_id: str                      # = LangGraph thread_id
status: intake|extracting|screening|assessing|awaiting_officer|awaiting_documents|executing|approved|rejected|failed
applicant: {name, aliases[], dob, nationality, residence_country, occupation}    # synthetic
documents: [{doc_ref, doc_type, sha256, kyc_document_id|None}]                   # never bytes
extraction: {attempted, available (every provided doc extracted), fields: [{document, name, value, confidence, needs_review, reason}],
             failed_documents[], doc_flags[]}
missing_documents: [doc_type]
screening: {list_source, snapshot_date, algorithm, threshold, hits: [{entry_id, matched_name, score,
            classification: strong|possible, field_agreement: {dob, nationality: agree|partial|disagree|unknown}, reason, llm_note|None,
            disposition: None|cleared|confirmed, disposition_by}]}
risk: {rating: low|medium|high, fired_rules: [{rule_id, severity, inputs, explanation}]}
recommendation: {action: approve|reject|request_info|manual_review, explanation, drafted_by: llm|template}
summary: str|None                 # officer summary (LLM or template)
missing_doc_draft: str|None       # never sent automatically
decision: {action: approve|reject|request_more_info, officer, note, at}|None
execution: {customer_id, idempotency_key}|None
info_rounds: int                  # capped (MAX_INFO_ROUNDS)
degraded: [str]                   # "extraction_unavailable", "llm_unavailable", ...
audit_head: str                   # row_hash of the last audit row written for this case
```

## Hard rules
- **The LLM never decides.** Sanctions matching, risk scoring, the recommendation action and the final decision path are
  deterministic Python or human. The LLM may only: (1) summarise the case for the officer; (2) explain a recommendation
  using the rule outputs it was given; (3) draft the missing-document request; (4) optionally annotate a fuzzy hit as
  "likely false positive because...". The annotation is advisory, logged, and never clears a hit. Only an officer disposition
  clears one. No code path may read LLM output to choose a route, a rating, or a hit status.
- **No auto-approval path exists.** `execute` is reachable only from `approve` with a human decision on the checkpoint.
- **Synthetic data only.** No real ID numbers, no real people as applicants. Sanctions-hit fixtures are synthetic applicants
  with deliberate fuzzy variants of public-list names. Every fixture and generated document is marked `SPECIMEN`.
- **Sanctions list is vendored**, dated, in `data/sanctions/`. Never fetched at runtime in prod. Every screening result
  records source + snapshot date.
- **No PII in traces or logs** beyond `case_id` and synthetic names. Uploaded documents are never persisted (hash only,
  as in P3). Prompts sent to the LLM and Langfuse payloads carry no DOB, ID number or address.
- **Audit log is append-only, enforced in Postgres**: trigger rejects UPDATE/DELETE/TRUNCATE, the app role has INSERT/SELECT
  only, each row stores `prev_hash` + `row_hash` (SHA-256 chain), `verify_audit_chain` proves integrity.
- **Degrade, don't fail.** KYC down: case reaches the officer flagged "extraction unavailable". LLM down: rules and screening
  still run, summary falls back to a template. Langfuse down: tracing off, prompts from `prompts/`.
- **Tests:** every node, every risk rule, the screening scorer, the audit chain, resume-after-interrupt. The graph is tested
  with a fake LLM. A test fails if any route function reads LLM-produced fields.
- **Keep it thin.** No Argo CD, no GitLab CI, no new security tooling beyond what P3's pipeline already runs.
- **Honesty:** never invent metrics. Every number in README/docs comes from a file committed in `evals/results/`. Never cite
  P3's `app/eval/results/` (its committed run is all failed calls). Never quote CBUAE clause numbers unless read from the
  official text (see CONTROLS.md).

## Branching (same model as P3)
`main` = production, `qa` = integration branch cut from `main`, `feature/*` (and `bugfix/*`) cut from `qa`. Never branch a feature from
`main`, never push directly to `main` or `qa`. Flow: push the feature branch, PR into `qa`; after the merge succeeds the QA pipeline
runs (build, scans, ECR push, deploy to `onboarding-qa`). Then a PR `qa` into `main`; after that merge succeeds the prod pipeline runs
(approval-gated retag and deploy to `onboarding-prod`). `hotfix/*` is cut from `main` and merged into `main` and `qa`. Names are
lowercase `<type>/<ticket-id>-<short-slug>`, for example `feature/onb-001-branching-strategy`; releases are tagged `vMAJOR.MINOR.PATCH`.
Until Phase 4 the only workflow is `ci.yml` (Gitleaks, lint, types, tests), which runs on PRs and pushes to `qa` and `main`; nothing deploys.

## Things that will bite you
- **interrupt() re-runs the node from the top on resume.** Nothing before `interrupt()` may have side effects (audit writes,
  LLM calls, HTTP). Put the gate in its own node. Resume payloads are checkpointed: never put document bytes or PII in them.
- **A resume decision is bound to the interrupt it answered** (MIA D-50): payload carries `interrupt_id`; stale or duplicate
  resumes are dropped and audited. Resume is compare-and-set on `status`.
- **Document bytes are not in state.** They live in an in-process buffer keyed by `case_id` until `extract` consumes them. A
  crash before extract means the client must re-upload; the case then shows "extraction unavailable" (no silent retry).
- **Idempotency:** `execute` sends `Idempotency-Key: <case_id>`; mock-bank has a UNIQUE constraint on it and replays the stored
  response. Write the audit row `execute_requested` before the call and `execute_completed` after; a crash between them
  replays safely. A different payload under the same key returns 409.
- **List snapshot dates:** screening results are only comparable within a snapshot. Re-screening on resume uses the case's
  original snapshot unless the officer explicitly requests a re-screen (audited).
- **Bedrock quota:** P3's account has per-minute quota 0 for Anthropic models until raised; calls throttle (the deployed KYC
  service returns 502). Run LLM calls sequentially with exponential backoff and jitter; support a cross-region inference
  profile via `BEDROCK_MODEL_ID`. P3 uses `in.` (India-only) profiles: never silently switch to `global.`.
- **Own namespaces on P3's cluster:** `onboarding-qa` / `onboarding-prod`, created by `infra/terraform` (D-03, D-10). P3's cluster,
  operators, ALB group and Route 53 zone are read via data sources and never modified. Destroying our stack must not touch P3.
- **P3 scans are report-only** (`ENFORCE_SCANS: "false"`); only Gitleaks hard-fails. Do not claim Trivy/Checkov/Sonar as
  blocking gates unless we flip it in our copy.
- LangGraph strict msgpack: keep state to pydantic models/primitives; set `LANGGRAPH_STRICT_MSGPACK=true` (MIA D-24).
- **The audit log refuses personal-data keys** (`dob`, `id_number`, `value`, `address`...) and long strings: name payload keys accordingly
  (`dob_agreement`, not `dob`). Rule `inputs` appear in audit rows, so keep them to identifiers, countries and counts.
- **Paths:** use `onboarding.paths` (env `ONBOARDING_DATA_DIR`, `ONBOARDING_PROMPTS_DIR`) never `Path(__file__)` tricks: the image installs
  the package into a venv.
- Hash chain concurrency: appends take `pg_advisory_xact_lock`; never insert audit rows from two connections without it.
