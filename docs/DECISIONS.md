# Decisions

Format follows MIA's `docs/DECISIONS.md` (decision, why, status). All entries are **Proposed** until the user approves them.
D-01 to D-07 are the seven open decisions from the brief (D-01 repo, D-02 DB, D-04 pipeline, D-05 sanctions source,
D-06 fuzzy matching, D-07 Langfuse, D-09 UI; D-03 and D-08 are new and D-10 onward came out of reading P3 and MIA).

## Reused from reference repos

| Pattern | Taken from | What we reuse / what we change |
|---|---|---|
| `interrupt()` approval gate, node re-runs from top on resume | MIA `app/graph/nodes.py` `approve_gate`, ADR 0002 | Same semantics. Our payload is a case summary; decision has three actions. |
| Postgres checkpointer, `thread_id` = job id, own schema, pool, strict msgpack | MIA `app/graph/checkpoint.py`, ADR 0003, D-24 | `thread_id` = `case_id`. No Entra token auth for the DB. |
| Resume bound to the interrupt it answered; compare-and-set status | MIA D-28, D-50 | Same idea, keyed by `interrupt_id`. |
| Recovery at startup resumes from checkpoint | MIA ADR 0003 | Re-implemented without arq/Redis (D-12). |
| "Deterministic Python overrides the LLM" | MIA D-10, D-17, D-18 | Wider: the LLM has no decision role at all. |
| "Degrade, don't fail" | MIA D-31, `data_gaps` set by Python | `degraded[]` set by Python; KYC and LLM failures degrade. |
| Retry layer: 429/5xx/timeouts only, one layer | MIA `app/resilience.py`, D-29 | Port the policy to boto3 `ThrottlingException`; botocore retries off. |
| Append-only audit table with trigger | MIA D-65 | **Extended**: hash chain, INSERT-only role, verify command (MIA has no chain). |
| Separation of duties: submitter cannot approve | MIA D-62 | Same rule, simpler identity (D-08). |
| Langfuse trace id derived from job id; trace per run; spans per node via callback handler; `observe()` child spans | MIA `app/observability.py` | Trace id derived from `case_id`; tracing never fails a case. |
| ADR/DECISIONS discipline, KNOWN_LIMITATIONS | MIA `docs/` | Same. |
| Strict-CSP review page, text-only rendering, test that bans `innerHTML` etc. | P3 `app/kyc/main.py` UI_HEADERS, `tests/test_ui.py` | Server-rendered templates instead of vanilla JS SPA. |
| Hash-only document handling, redacting log filter and test that reads real log output | P3 `kyc/pii.py`, README privacy section | Same. |
| Release pipeline shape: scans, build once, ECR push by SHA, deploy, commit tag, retag to `prod-sha` behind an Environment | P3 `.github/workflows/` | Copied and adapted (D-04). |
| Per-env namespaces, ESO + IRSA, shared ALB group, kustomize with image rewrite at deploy time | P3 `k8-manifests/`, `terraform/modules/app-env` | Reused. |

**Not reusable as-is (MIA):** prompts live in code (`prompts.py`), not in Langfuse prompt management, so prompt versioning
is net-new here. MIA runs on Azure (Azure OpenAI, Key Vault, Container Apps, Helm); only the patterns carry over.

---

### D-01 · Repo name
- **Context:** The user supplied `https://github.com/meetbrij/client-onboarding-ai-agent`.
- **Recommendation:** Use it. Local folder is `ai-client-onboarding`, remote is `client-onboarding-ai-agent`; harmless. Python
  package `onboarding`. ECR repo `client-onboarding`. K8s resources prefixed `onboarding-`.
- **Consequence:** The new repo has a different GitHub OIDC `sub` (owner@id/repo@id form, per P3 CLAUDE.md), so P3's deploy
  roles will not trust it. See D-03.
- **Status:** Proposed (effectively decided by the user).

### D-02 · Checkpointer and audit database: in-cluster Postgres StatefulSet
- **Options:** (a) Postgres StatefulSet in qa/prod; (b) reuse P3's MySQL.
- **Recommendation:** (a). LangGraph ships a first-party Postgres saver (as MIA uses) and no MySQL one. The audit trigger and
  advisory-lock hash chain are straightforward in Postgres. A custom MySQL saver would be the largest piece of unreviewed code
  in the project, and the brief says keep it thin.
- **Cost:** a second StatefulSet, secret (`<env>/pg-secret` in Secrets Manager), ESO policy extension, 5Gi PVC, about 250m CPU
  and 512Mi RAM requested (D-10).
- **Roles:** `onboarding_app` (CRUD on non-audit tables, INSERT/SELECT on `audit_log`), `onboarding_owner` (migrations only).
- **Limit to document:** a Postgres superuser can drop the trigger. The chain detects the edit afterwards; tail truncation is
  only detectable if the head hash is anchored elsewhere (we write it to the case trace and a log line at case end).
- **Status:** Proposed.

### D-03 · Infrastructure changes live in a small Terraform overlay in this repo, not in P3
- **Context:** P3's CLAUDE.md says the pipeline is frozen and its ECR repo is a single immutable `nodejs-app` repo. P3's
  `app-env` module creates deploy roles bound to P3's repo claim, ESO roles limited to `<env>/mysql-secret`, and a Bedrock
  IRSA role (`kyc-app`) limited to the two `in.` profiles.
- **We need:** our own ECR repo, deploy roles trusting our repo (qa: `ref:refs/heads/qa`; prod: `environment:prod`), an IRSA
  role for our service account to invoke Haiku 4.5, secret shells (`pg-secret`, `langfuse-keys`, officer tokens), ESO read
  access to them.
- **Recommendation:** `infra/terraform/` here, reading P3's remote state (`platform` outputs: cluster, OIDC provider ARNs) and
  referencing the namespaces as data sources. P3 stays untouched except, at most, a quota bump (D-10) and an ACM SAN for our
  hostnames, which are tiny `tfvars` changes the user applies.
- **Alternative:** extend P3's `app-env` module. Tighter reuse, but edits a frozen repo and couples two projects' blast radius.
- **Status:** Proposed.

### D-04 · Pipeline reuse: copy, do not `workflow_call`
- **Verified:** P3's `qa-cicd.yml` and `prod-cd.yaml` have no `workflow_call` trigger; they hardcode `IMAGE_NAME: nodejs-app`,
  cluster, namespace, `PROD_HOST`, a `matrix.app: [app]`, and `k8-manifests/qa/kustomization.yaml` paths. The prod workflow
  reads the QA tag from the repo it runs in. The OIDC role comes from repo variables of the calling repo, and the AWS trust is
  per repo.
- **Options:** (a) refactor P3 to expose reusable workflows; (b) copy and adapt.
- **Recommendation:** (b). (a) means changing a pipeline that P3 declares frozen, and a caller-side repo still needs its own
  roles and variables. Copy the two files, replace the app-specific values by `env:` at the top, and put a comment with the P3
  commit SHA copied from. Add one job: the deterministic eval gate after tests.
- **Honest consequence:** two copies can drift. Accepted; documented in KNOWN_LIMITATIONS.
- **Status:** Proposed.

### D-05 · Sanctions source: UN Consolidated List at launch; OFAC SDN as a stretch
- **Reasoning:** UN lists are what UAE institutions are directly required to act on, they are free, and the XML carries
  aliases, DOB (several formats) and nationality, which the corroboration logic needs. OFAC SDN matters for USD correspondent
  banking but its DOB and nationality live in free-text remarks in the classic CSV (the Advanced XML is structured but heavier).
- **Recommendation:** UN only for the eval set and the demo; the loader writes a source-neutral normalised schema
  (`entry_id, source, names[], aliases[], dobs[], nationalities[], list_date`) so adding OFAC is a loader plus a manifest entry.
  Add OFAC only if Phase 3 finishes early. This also keeps the claim honest ("screens against the UN Consolidated List").
- **Needs checking at build time:** current download URL and license/terms of the UN list file, recorded in the manifest.
- **Status:** Proposed.

### D-06 · Fuzzy matching: rapidfuzz `token_sort_ratio` plus corroboration
- **Recommendation:** Normalise (Unicode NFKD, casefold, strip punctuation and honorifics, collapse whitespace) then score each
  applicant name and alias against each list name and alias with `rapidfuzz.fuzz.token_sort_ratio`; also compute
  `token_set_ratio` as a diagnostic only. Raise a hit at name score >= 85 (start value, see below).
  Corroboration is a separate, deterministic step: DOB `exact | year_only | mismatch | unknown`, nationality
  `agree | disagree | unknown`. Classification: `strong` (score >= 92 and no disagreement), `possible` (otherwise raised), and
  a DOB mismatch demotes `strong` to `possible` but never removes the hit (an officer must clear it).
- **Why not more:** phonetic and Arabic-aware transliteration matching would improve recall for real UAE names, but it adds
  scope. Listed as a known limitation; this is a demo-scale scorer, not a screening-vendor replacement.
- **Threshold honesty:** thresholds are tuned on a separate dev set of 10 variant names, not on the 12 eval cases, and the
  chosen values are recorded in `data/reference/screening_config.yaml` with the dev-set result. With 12 eval cases, precision and
  recall are illustrative, not statistical; EVALS.md says so.
- **Status:** Proposed.

### D-07 · Langfuse: Cloud for now, self-host not recommended
- **Reasoning:** Self-hosting Langfuse v3 means ClickHouse, Redis, an object store and web/worker pods, on a 2-node t3a.large
  cluster already carrying qa, prod and the observability stack. That is more operational work than the rest of this project.
  Langfuse Cloud is free to start, and MIA already uses it (`cloud.langfuse.com`).
- **Residency:** traces and prompts leave AWS. With the PII rules (case_id + synthetic names only, no DOB, no ID numbers, no
  document data) this is acceptable for a portfolio build. CONTROLS.md states that a real bank would use self-hosted
  Langfuse in-region or a UAE/EU-region cloud project, and that the vendor review (third-party control) applies.
- **Region:** pick the region at project creation (EU or US are what the cloud offered when MIA was built); verify the current
  options.
- **Fallbacks:** if Langfuse is down, tracing is off and prompts come from `prompts/` (version recorded as
  `local-fallback`).
- **Status:** Proposed.

### D-08 · Officer identity and separation of duties: static per-officer API tokens
- **Context:** MIA uses Entra ID; we have no IdP in P3.
- **Recommendation:** per-officer bearer tokens (hashed) from a Secrets Manager secret, roles `submitter` and `officer`,
  `decided_by` recorded in the audit row; an officer cannot decide a case they submitted (MIA D-62). The UI states that this is
  a stand-in for the bank's SSO. Cognito/OIDC is the production answer and is listed as out of scope.
- **Alternative:** no auth, API key only (P3's pattern). Weaker: no attributable human decision, which is the core control.
- **Status:** Proposed.

### D-09 · Officer UI: yes, minimal and server-rendered
- **Recommendation:** Build it (Phase 3), because a human-approval workflow is much easier to judge when you can see the
  officer page. Jinja templates, no JS framework, strict CSP (`default-src 'none'`, self-hosted CSS, form posts with a CSRF
  token), values rendered as text. About four pages. It is cut item 4 if behind.
- **Status:** Proposed.

### D-10 · Share P3's `qa` and `prod` namespaces; measure capacity first
- **Verified:** P3's `app-env` module sets a per-namespace ResourceQuota (defaults: 1 CPU / 2Gi requests, 3Gi limits, 20 pods,
  3 PVCs, 10Gi storage; prod overrides storage to 20Gi). Existing requests: qa about 350m / 704Mi; prod about 450m / 896Mi
  (2 KYC replicas plus MySQL). The cluster is 2 x t3a.large carrying monitoring too.
- **Our requests (estimate):** agent 100m / 192Mi, mock-bank 50m / 96Mi, Postgres 250m / 512Mi. Prod then sits near 850m / 1.7Gi
  of 1 CPU / 2Gi. Tight but feasible; qa PVC storage hits exactly 10Gi.
- **Recommendation:** share the namespaces (the brief and P3's IAM scoping assume them), keep requests small, and raise the
  quota by `tfvars` (D-03) if Phase 4 measurement shows pressure. Alternative is `onboarding-qa/-prod` namespaces, which
  needs new deploy-role scoping.
- **Status:** Proposed.

### D-11 · One image, two Deployments
- **Reason:** P3's contract is "single image per commit"; more images would be a pipeline change. mock-bank is a few hundred
  lines. Same image, `command:` selects `onboarding-api` or `onboarding-mock-bank`.
- **Consequence:** the mock bank is not independently versioned. Acceptable: it is a mock.
- **Status:** Proposed.

### D-12 · Run the graph in the API process; recover at startup; no Redis/arq
- **Reasoning:** MIA's arq worker adds Redis and a second process. Case runs here take seconds, not minutes, and pause on a
  human. An asyncio task per case plus a startup recovery scan (re-invoke cases in non-terminal, non-waiting states from their
  checkpoint) gives crash-resume with fewer moving parts.
- **Trade-off:** one API replica assumed for recovery (MIA D-25 has the same limit). Use an advisory lock per case so two
  replicas cannot run the same case.
- **Status:** Proposed.

### D-13 · Document bytes: in-process buffer, never in state or DB
- **Context:** P3's KYC `POST /documents` takes multipart bytes, processes in memory, stores only SHA-256, extracted values
  and confidences. LangGraph checkpoints every state update (and resume values), so bytes must never enter state.
- **Decision:** the API reads bytes into a TTL buffer keyed by `(case_id, doc_ref)`; `extract` consumes them. If the process
  dies first, the buffer is gone: extraction is marked unavailable and the officer sees it. "Request more info" waits at a
  second interrupt and new documents arrive as `POST /cases/{id}/documents`, which fills the buffer then resumes with hashes only.
- **Honest note:** P3 does persist extracted field values (name, DOB, ID number) in its MySQL. "Hash-only" is true of the
  files, not of the extracted fields. We store extracted values in the checkpoint (needed for screening and the officer page,
  synthetic data only) and keep them out of logs, traces, LLM prompts and audit payloads (audit stores field names and
  confidences, not values).
- **Status:** Proposed.

### D-14 · Recommendation set and approval guard
- **Decision:** `recommendation.action` in `approve | reject | request_info | manual_review`, computed by Python from the rule
  results with this precedence: a `strong` hit gives `reject` (officer disposition still mandatory); else missing documents
  give `request_info`; else any needs-review field, `possible` hit, unavailable extraction or medium/high rating gives
  `manual_review`; else `approve`. This matches the table in EVALS.md. The officer chooses approve / reject / request_more_info. A guard blocks `approve` while any
  hit lacks an officer disposition or extraction was unavailable. The officer may always reject or request more info.
- **Why:** it keeps the "LLM never decides" rule testable, gives CBUAE-style human oversight, and no auto-approval path exists
  (even a clean case needs a human click).
- **Status:** Proposed. Rating aggregation (max severity) and the rule thresholds are specified in `rules/README` in Phase 2.

### D-15 · Evals use recorded KYC responses, not the live service
- **Reason:** P3's accuracy eval is blocked on Bedrock quota, and its committed `app/eval/results/RESULTS.md` is an all-failed
  run (0 of 18 documents processed). We cannot depend on the live KYC service for the eval, and we must not cite P3's numbers.
- **Decision:** each eval case carries a KYC response shaped exactly like P3's `DocumentOut` (`fields[]` with `confidence`,
  `needs_review`, `reason`; document `status`). EVALS.md states that extraction accuracy is out of scope and is P3's metric.
  One cluster smoke test calls the real KYC service.
- **Status:** Proposed.

### D-16 · Manifests: kustomize, not Helm
- **Reason:** P3's pipeline uses `kubectl apply -k` and rewrites the image in `kustomization.yaml`; MIA's Helm chart targets a
  different platform. Matching P3 means no pipeline redesign. The brief says "manifests/Helm"; this chooses manifests.
- **Status:** Proposed.

### D-17 · Prompts: Langfuse prompt management, four prompts, label-driven
- **Prompts:** `onboarding-summarise-case`, `onboarding-explain-recommendation`, `onboarding-draft-missing-docs`,
  `onboarding-annotate-hit`. Labels `production`, `staging`. Code fetches by label with a short TTL cache, falls back to the
  checked-in `prompts/*.txt`. Each LLM call records `{prompt_name, prompt_version, model_id, tokens}` in the audit row and the
  Langfuse generation. Evals pin an explicit version and record it in the result file. Prompts receive only rule outputs and
  hit reasons, never raw PII.
- **Status:** Proposed.

### D-18 · The "true hit" fixture and "no real people"
- **Tension:** the brief bans real people as applicants but wants a true-hit case built from public-list names. A true hit
  must echo a listed person's name, and for DOB/nationality corroboration, their listed DOB and nationality.
- **Recommendation:** use the listed entry's name (or a small variant), DOB and nationality from the public record in the
  fixture's applicant block only; everything else (documents, ID number, address, occupation) is invented and marked
  `SPECIMEN`; the applicant is a fixture, not presented as that person. Prefer an old, well-known entry. Never use these
  fixtures in screenshots without the SPECIMEN banner. If this feels wrong, switch to name-only variants and drop DOB/nationality
  corroboration from the true-hit case (the DOB-mismatch case still covers corroboration).
- **Status:** Proposed.
