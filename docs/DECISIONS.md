# Decisions

Format follows MIA's `docs/DECISIONS.md` (decision, why, status). Status is **Proposed** until the user accepts it; accepted entries are marked.
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
  package `onboarding`. ECR repo `client-onboarding` (D-11). K8s resources prefixed `onboarding-`.
- **Consequence:** The new repo has a different GitHub OIDC `sub` (owner@id/repo@id form, per P3 CLAUDE.md), so P3's deploy
  roles will not trust it. See D-03.
- **Status:** Accepted (user, 2026-10-05).

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
- **Status:** Accepted (user, 2026-10-06).

### D-03 · Our own Terraform stack in this repo; P3 is read, never modified (user-approved, expanded)
- **Context:** P3's pipeline is frozen and its ECR repo is a single immutable `nodejs-app`. Its `app-env` module creates
  deploy roles bound to P3's repo claim, ESO roles limited to `<env>/mysql-secret`, and a Bedrock IRSA role limited to its two
  `in.` profiles.
- **Decision:** `infra/terraform/` (own state key, own backend config) creates everything this project owns:
  - ECR repo `client-onboarding` (immutable tags, scan on push, lifecycle like P3's);
  - namespaces `onboarding-qa` / `onboarding-prod` with quotas (D-10);
  - deploy roles trusting this repo: qa `ref:refs/heads/qa`, prod `environment:prod` (using the immutable-ID subject form
    from P3's CLAUDE.md, read from a real token if it fails), with EKS access entries scoped to our namespaces and a Role for
    ESO CRDs, as P3 does;
  - an IRSA role and service account for Bedrock (`bedrock:InvokeModel` on the Haiku 4.5 profile and its underlying
    models only, same condition pattern as P3);
  - Secrets Manager secret shells `<env>/onboarding/pg-secret`, `.../langfuse-keys`, `.../officer-tokens` (values set
    out-of-band, never in state), plus an ESO IAM role per environment that can read only those;
  - its own ACM certificate for our two hostnames, DNS-validated in the shared Route 53 zone (the ALB controller finds
    certificates by hostname, so P3's certificate does not need a new SAN);
  - Route 53 alias records for the hostnames, created by Terraform once the ALB exists (data lookup), or by hand like P3
    if that proves awkward.
- **Hostnames (proposal):** `qa-proj4-onboarding.bolarbrijesh.com` and `proj4-onboarding.bolarbrijesh.com`, following P3's
  `qa-proj3-aigateway` / `proj3-aigateway` pattern. Zone: data source, never created or destroyed.
- **How we read P3:** `data` sources by cluster name (`aws_eks_cluster`, OIDC provider ARN from the cluster), not
  `terraform_remote_state`, so we don't depend on P3's state bucket layout or credentials.
- **Independence:** `terraform destroy` here removes only our resources. P3 is unaffected, except that our Ingresses leave the
  shared ALB group.
- **Status:** Accepted (user approved the scope on 2026-10-05); namespace part follows D-10.

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
- **Status:** Accepted (user, 2026-10-05).

### D-05 · Sanctions source: UN Consolidated List at launch; OFAC SDN as a stretch
- **Reasoning:** UN lists are what UAE institutions are directly required to act on, they are free, and the XML carries
  aliases, DOB (several formats) and nationality, which the corroboration logic needs. OFAC SDN matters for USD correspondent
  banking but its DOB and nationality live in free-text remarks in the classic CSV (the Advanced XML is structured but heavier).
- **Recommendation:** UN only for the eval set and the demo; the loader writes a source-neutral normalised schema
  (`entry_id, source, names[], aliases[], dobs[], nationalities[], list_date`) so adding OFAC is a loader plus a manifest entry.
  Add OFAC only if Phase 3 finishes early. This also keeps the claim honest ("screens against the UN Consolidated List").
- **Needs checking at build time:** current download URL and license/terms of the UN list file, recorded in the manifest.
- **Status:** Accepted (user, 2026-10-05).

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
- **Status:** Accepted (user, 2026-10-05).

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
- **Status:** Accepted (user, 2026-10-05).

### D-08 · Officer identity and separation of duties: static per-officer API tokens
- **Context:** MIA uses Entra ID; we have no IdP in P3.
- **Recommendation:** per-officer bearer tokens (hashed) from a Secrets Manager secret, roles `submitter` and `officer`,
  `decided_by` recorded in the audit row; an officer cannot decide a case they submitted (MIA D-62). The UI states that this is
  a stand-in for the bank's SSO. Cognito/OIDC is the production answer and is listed as out of scope.
- **Alternative:** no auth, API key only (P3's pattern). Weaker: no attributable human decision, which is the core control.
- **Status:** Accepted (user, 2026-10-05).

### D-09 · Officer UI: yes, minimal and server-rendered
- **Recommendation:** Build it (Phase 3), because a human-approval workflow is much easier to judge when you can see the
  officer page. Jinja templates, no JS framework, strict CSP (`default-src 'none'`, self-hosted CSS, form posts with a CSRF
  token), values rendered as text. About four pages. It is cut item 4 if behind.
- **Status:** Accepted (user, 2026-10-05).

### D-10 · Namespaces: our own (`onboarding-qa`, `onboarding-prod`), on P3's cluster (revised)
- **Why the original recommendation (share P3's `qa`/`prod`) no longer makes sense:** with a separate Terraform stack (D-03),
  shared namespaces break independent teardown. The Postgres StatefulSet, PVCs, Deployments and Ingresses are applied by the
  pipeline (`kubectl apply -k`), not Terraform, so `terraform destroy` on our stack would leave them orphaned inside P3's
  namespaces, and the quota there is owned by P3's state. Sharing also means our Postgres competes with P3's MySQL for P3's
  quota (verified: defaults 1 CPU / 2Gi requests, 20 pods, 3 PVCs; prod near 450m / 896Mi already).
- **Recommendation:** our Terraform stack creates `onboarding-qa` and `onboarding-prod` with their own ResourceQuota and
  LimitRange, ESO SecretStore service account, and the deploy-role access entries scoped to those namespaces. Destroying the
  stack deletes the namespaces and everything in them. The brief's "qa and prod" is satisfied as environments; only the
  namespace names differ.
- **What we still share from P3 (read via data sources, never modified):** the EKS cluster and its nodes, the OIDC provider
  (IRSA), the GitHub OIDC provider (account-wide), the External Secrets and ALB controller operators, the EBS CSI driver and
  StorageClasses (`ebs-sc`, `ebs-sc-retain`), the shared ALB ingress group, Tempo/Prometheus/Loki, the Route 53 zone.
- **Capacity:** two t3a.large nodes carry P3 and monitoring already. Measure free allocatable CPU/memory before Phase 4. If
  short, the fix is a node-group size bump, which is a P3 change; say so then.
- **Hard dependency to document:** destroying P3's platform stack destroys the cluster under us. Independence is of
  *teardown of our resources*, not of the cluster's existence.
- **Alternative (a fully separate cluster):** true isolation, but roughly the cost of another EKS control plane (about $73 a
  month at list price, check current pricing) plus nodes and NAT, duplicated add-ons and observability, and the KYC call
  would no longer be in-cluster. Not recommended for a portfolio project.
- **Status:** Accepted (user, 2026-10-06).

### D-11 · One image, two Deployments (user decision, 2026-10-06; supersedes an earlier two-image choice)
- **Decision:** one image built from one `Dockerfile`; two Deployments select the process by `command:` (`onboarding-api` or
  `onboarding-mock-bank`). The user chose this to keep the pipeline simple.
- **Benefits:** one build, one Trivy scan, one SBOM, one ECR repo, one retag; the pipeline stays close to P3's copy (D-04); the API
  and the mock bank always deploy in lockstep, so they cannot drift.
- **Trade-offs accepted:** the mock bank ships with the API's dependencies, so it has a larger attack surface and its scan
  includes packages it never uses; the mock cannot be rolled out or versioned independently; the separation is a process and
  Service boundary, not an image boundary. Because both start from the same code, keep `mock_bank/` free of imports from
  `onboarding/` (a test enforces it) so it stays a stand-in for an external system.
- **Kept anyway:** a contract test runs the API's bank client against the mock app in-process in CI.
- **Status:** Accepted (user, 2026-10-06).


### D-12 · Run the graph in the API process; recover at startup; no Redis/arq
- **Reasoning:** MIA's arq worker adds Redis and a second process. Case runs here take seconds, not minutes, and pause on a
  human. An asyncio task per case plus a startup recovery scan (re-invoke cases in non-terminal, non-waiting states from their
  checkpoint) gives crash-resume with fewer moving parts.
- **Postgres makes more than one replica safe:** a per-case `pg_advisory_lock` means only one process runs a given case at a
  time, whichever replica received the request (MIA D-25 had to assume a single worker). We still deploy one API replica per
  environment to stay within capacity (D-10); the design does not depend on that.
- **Status:** Accepted (user, 2026-10-06).

### D-13 · Document bytes: in-process buffer, never in state or DB *(partly superseded by D-24: originals are now kept in a document store; the buffer and "never in state" still hold)*
- **Context:** P3's KYC `POST /documents` takes multipart bytes, processes in memory, stores only SHA-256, extracted values
  and confidences. LangGraph checkpoints every state update (and resume values), so bytes must never enter state.
- **Decision:** the API reads bytes into a TTL buffer keyed by `(case_id, doc_ref)`; `extract` consumes them. If the process
  dies first, the buffer is gone: extraction is marked unavailable and the officer sees it. "Request more info" waits at a
  second interrupt and new documents arrive as `POST /cases/{id}/documents`, which fills the buffer then resumes with hashes only.
- **Honest note:** P3 does persist extracted field values (name, DOB, ID number) in its MySQL. "Hash-only" is true of the
  files, not of the extracted fields. We store extracted values in the checkpoint (needed for screening and the officer page,
  synthetic data only) and keep them out of logs, traces, LLM prompts and audit payloads (audit stores field names and
  confidences, not values).
- **Re-evaluated with Postgres (user asked, 2026-10-06):** the decision stands, with these refinements.
  - *Do not park bytes in Postgres.* Storing documents (even encrypted, with a TTL) would let a case survive a crash between
    upload and extraction, but it contradicts the rule that uploaded documents are never persisted, and a database dump would then
    hold identity documents. Rejected. A crash in that window shows "extraction unavailable" and the client re-uploads.
  - *Replicas:* the buffer is per process, but the process that receives an upload is the one that runs the graph segment that
    consumes it, under the case's advisory lock. Officer decisions need no bytes, so any replica may handle them. No sticky
    routing needed.
  - *Checkpoints keep copies of PII.* Every checkpoint row stores state, including extracted values, so values would otherwise
    persist in many rows forever. Add a retention job: purge checkpoint rows of terminal cases after a configured number of days
    (default proposal: 30). The audit log is unaffected because it holds no values (field names, confidences, hashes only).
  - *At rest:* check that the `ebs-sc` StorageClass encrypts volumes (not verified in P3's manifests; if not, our Postgres
    volume class sets `encrypted: "true"` or relies on account-level default encryption). Test data is synthetic regardless.
  - *Postgres roles:* the app role gets DELETE only on checkpoint tables (for the purge), never on `audit_log`.
- **Status:** Accepted (user, 2026-10-06), with the refinements above.

### D-14 · Recommendation set and approval guard
- **Decision:** `recommendation.action` in `approve | reject | request_info | manual_review`, computed by Python from the rule
  results with this precedence: a `strong` hit gives `reject` (officer disposition still mandatory); else missing documents
  give `request_info`; else any needs-review field, `possible` hit, unavailable extraction or medium/high rating gives
  `manual_review`; else `approve`. This matches the table in EVALS.md. The officer chooses approve / reject / request_more_info. A guard blocks `approve` while any
  hit lacks an officer disposition or extraction was unavailable. The officer may always reject or request more info.
- **Why:** it keeps the "LLM never decides" rule testable, gives CBUAE-style human oversight, and no auto-approval path exists
  (even a clean case needs a human click).
- **Status:** Accepted (user, 2026-10-06).

### D-15 · Evals deferred until P3's KYC service is live; then run end to end against it
- **Decision (user, 2026-10-05):** hold the eval harness and first run until P3's KYC service works (its Bedrock quota is
  still 0, so it returns 502, and its committed `app/eval/results/RESULTS.md` is an all-failed run: 0 of 18 processed; we must
  not cite it).
- **Consequence:** the eval phase moves out of Phase 3 into its own Phase 3b, gated on a green KYC `POST /documents`.
  Graph, rule and audit tests still run now, using a small recorded-KYC fake shaped like P3's `DocumentOut` (`fields[]` with
  `confidence`, `needs_review`, `reason`; document `status`). That is test scaffolding, not an eval.
- **Better eval when it runs:** cases send SPECIMEN documents to the live KYC service, so decision accuracy is measured end to
  end (extraction included). That needs our own generator for specimen documents carrying our synthetic names (P3's golden
  set cannot carry sanctions-variant names), a bit more work than recorded responses; EVALS.md notes it.
- **Cost of waiting:** the resume claim "decision and trajectory evals with measured numbers" and the README numbers are not
  true until Phase 3b. The CI eval gate also waits.
- **Status:** Accepted (deferral); details revisit at Phase 3b.

### D-16 · Manifests: kustomize, not Helm
- **Reason:** P3's pipeline uses `kubectl apply -k` and rewrites the image in `kustomization.yaml`; MIA's Helm chart targets a
  different platform. Matching P3 means no pipeline redesign. The brief says "manifests/Helm"; this chooses manifests.
- **Status:** Accepted (user, 2026-10-06).

### D-17 · Prompts: Langfuse prompt management, four prompts, label-driven
- **Prompts:** `onboarding-summarise-case`, `onboarding-explain-recommendation`, `onboarding-draft-missing-docs`,
  `onboarding-annotate-hit`. Labels `production`, `staging`. Code fetches by label with a short TTL cache, falls back to the
  checked-in `prompts/*.txt`. Each LLM call records `{prompt_name, prompt_version, model_id, tokens}` in the audit row and the
  Langfuse generation. Evals pin an explicit version and record it in the result file. Prompts receive only rule outputs and
  hit reasons, never raw PII.
- **Status:** Accepted (user, 2026-10-06).

### D-18 · The "true hit" fixture and "no real people"
- **Tension:** the brief bans real people as applicants but wants a true-hit case built from public-list names. A true hit
  must echo a listed person's name, and for DOB/nationality corroboration, their listed DOB and nationality.
- **Recommendation:** use the listed entry's name (or a small variant), DOB and nationality from the public record in the
  fixture's applicant block only; everything else (documents, ID number, address, occupation) is invented and marked
  `SPECIMEN`; the applicant is a fixture, not presented as that person. Prefer an old, well-known entry. Never use these
  fixtures in screenshots without the SPECIMEN banner. If this feels wrong, switch to name-only variants and drop DOB/nationality
  corroboration from the true-hit case (the DOB-mismatch case still covers corroboration).
- **Status:** Accepted (user, 2026-10-05).

### D-19 · Rating aggregation, and which country the jurisdiction rule reads
- **Context:** writing the fixtures exposed two gaps in the rule design. Case 8 (missing document, low-confidence field,
  residence in an increased-monitoring country) must rate **high**, but each of those rules is medium severity, so "max severity"
  alone would give medium.
- **Recommendation:** rating = highest severity among fired rules, **raised one level when three or more distinct rules fire**
  (capped at high). Severities: R-SAN-01 strong hit high; R-SAN-02 possible hit medium; R-JUR-01 FATF call-for-action high; R-JUR-02
  increased monitoring medium; R-OCC-01 higher-risk occupation medium; R-DOC-01 missing document medium; R-DOC-02 low-confidence or
  malformed field medium; R-DOC-03 extraction unavailable high. The jurisdiction rules read `residence_country` only. (Corrected in Phase 2: this entry first
  also named the ID document's `issuing_country`, but that mirrors nationality, so a DRC national resident in the UAE would fire R-JUR-02,
  which cases 2 and 11 must not.)
- **Status:** Accepted (user, 2026-10-06). The fixtures already assume it.

### D-20 · Branching: `main`, `qa` (from main), `feature/*` (from qa), PR-only
- **Decision (user, 2026-10-06):** as P3. Feature PR into `qa` triggers the QA pipeline on merge; PR `qa` into `main` triggers the prod
  pipeline on merge. The Phase 1 work was committed straight to `main`; `qa` was cut from that commit, so all three start identical.
- **Branch protection to configure on GitHub (not done by Claude):** `qa`: block force-push and deletion only (the QA pipeline pushes a
  bot commit with the deployed tag, as in P3); `main`: require a PR, 0 approvals, block force-push and deletion.
- **Status:** Accepted.

### D-21 · Phase 2 design choices (made while building; please review)
- **Scorer:** keeps every listed entry whose best name or alias score reaches the raise threshold; classification and corroboration follow D-06.
  Aliases of every quality ("Good" and "Low") are matched, and the name the KYC service read from the document is screened as well as the declared
  name. Thresholds stay at 85 / 92: on the development set, 85 raised one false positive in 25 invented names and caught 8 of 10 variants
  (the two misses omit a middle name); 90 gave the same recall with no false positive. Screening favours recall, so 85 was kept, and the numbers are
  recorded in `data/reference/screening_config.yaml`. Revisit if officers see too many false positives.
- **LLM calls happen before the approval pause:** annotation in `screen`, explanation, summary and draft in `assess`, so the interrupted node stays
  free of side effects. A per-run circuit breaker stops calling the model after the first outage in a node.
- **Outputs are checked, not trusted:** explanation and summary cannot name a rule that did not fire; a draft must ask for exactly the missing
  documents and must not mention screening, risk, rules or a decision; failures fall back to templates and are audited as `llm_output_rejected`.
- **Template wording lives with the LLM package** (`llm/facts.py`), so `rules/` and `screening/` contain nothing about LLM-produced fields; tests
  enforce that they do not import `onboarding.llm`.
- **The trajectory is read from the audit log:** `node_completed` rows in order, plus `approval_requested` (written by the runner, not the
  interrupted node) as `approve`.
- **Strict checkpoint deserialisation:** `LANGGRAPH_STRICT_MSGPACK=true` with an allowlist of exactly the classes in `onboarding.models`.
- **Status:** Proposed; waiting for the user.

### D-22 · Phase 3 design choices (made while building; please review)
- **One pause, one answer.** A pause has a deterministic id (`<case>:a<round>` for the officer, `<case>:d<round>` for documents). The service refuses
  a decision or upload that answers any other pause. The order is: check, audit `decision_received` (if this fails nothing is applied), claim the pause
  with a compare-and-set on the `cases` row and store the claimed decision, then resume the graph from the checkpoint. A crash after the claim is
  finished by `recover()` at the next start (from any replica). This reuses MIA D-50 and D-28 but is simpler than MIA (no queue).
- **The guard is stricter than the brief:** approval needs a disposition on every hit, no confirmed match, available extraction and complete documents,
  and a note of at least 10 characters when it goes against the recommendation, above low risk, or after clearing a hit (the override rationale a
  reviewer wants). Rejecting a case with a strong hit also needs a disposition. The `approve` node re-checks as a second line of defence.
- **`cases` table as a projection** (status, who submitted, current pause, a claimed decision) next to LangGraph's checkpoints; it makes listing cheap and
  the claim atomic. It holds the synthetic applicant name only. `final_json` keeps the outcome after a retention purge.
- **Submitters see status only.** Showing a client their screening result or risk rating could tip them off, so the API and UI hide them from the
  `submitter` role. Officers see everything. Nobody decides a case they submitted.
- **Database roles:** `owner` migrates; `app` has SELECT/INSERT on `audit_log` (and nothing else on it), SELECT/INSERT/UPDATE on `cases`, and
  SELECT/INSERT/UPDATE/DELETE on LangGraph's tables so the retention purge works. Tests prove each of these.
- **Fixture and model changes made because of the guard and the edge:** the `high_risk_occupation` case now carries an approval note; the applicant model rejects
  implausible birth dates at the edge (so `intake`'s own date check is a second line).
- **Errors carry no applicant data** into `cases.last_error`, the audit log or logs: the exception class only.
- **Langfuse:** trace id derived from the case id; a root span per run (`case:start`, `case:resume`, `case:recover`), a span per node, tool spans for the
  KYC and bank calls, a generation per LLM call linked to the prompt version when the prompt came from Langfuse. Prompts: label `production` or `staging`
  (`PROMPT_LABEL`), SDK cache 60 s, local copy when Langfuse is unreachable. `scripts/sync_prompts.py` creates a new prompt version only when the text differs.
- **Dev tokens exist only in `dev`:** qa and prod refuse to start without `ONBOARDING_TOKENS` and `SESSION_SECRET`, and prod refuses the fake LLM.
- **Status:** Proposed; waiting for the user.

### D-23 · Phase 4 design choices (made while building; please review)
- **Kustomize base and overlays** (`k8s/base`, `k8s/qa`, `k8s/prod`) instead of P3's two copied directories: one definition of Postgres, the API, the mock
  bank and the network policies; the overlays set the environment name, the KYC URL, the ExternalSecret paths, the Ingress host, and (prod) the volume class and size.
  The pipeline reads and writes `newName`/`newTag` in the overlay exactly as P3's does. Patches match env vars by name, not by position.
- **Migrations run in an init container** of the API pod (owner role, idempotent, advisory-locked), so a deploy never needs a separate job and the running app keeps
  only the restricted role. Passwords reach connection URLs through Kubernetes `$(VAR)` expansion; they are generated hex strings (no characters that need encoding).
- **Postgres in the cluster is a single-pod StatefulSet** with the same hardening as the other pods (non-root uid 999, read-only root, no capabilities), an init script
  from a ConfigMap that creates the roles and databases from secret values, `ebs-sc` in qa and `ebs-sc-retain` (10Gi) in prod.
- **The LLM backend in the cluster is Bedrock in both environments**, with `LLM_RETRY_ATTEMPTS=2` so that, while P3's account has zero Bedrock quota, a case takes
  seconds (not a minute) before falling back to templates. Prod refuses the fake backend. Switching the model off is `LLM_ENABLED=false`.
- **Three Secrets Manager secrets per environment** (`pg-secret`, `app-secret`, `langfuse-keys`); the smoke-test token is a submitter-only token in `app-secret`, read
  by the pipeline with the deploy role (the namespace-scoped Edit policy allows reading secrets in that namespace only).
- **Separate states** like P3's (`platform`, `envs/qa`, `envs/prod`), each with a partial S3 backend config and `use_lockfile`, so qa and prod can be applied or destroyed
  on their own.
- **Workflow changes from P3's:** `ci.yml` is replaced by `qa-cicd.yml` (which also runs on PRs into `main`, a small extension), tests and lint use uv with a Postgres
  service, the offline fixture run is a CI step, SonarCloud is off until `SONAR_ENABLED=true`, and a smoke test follows the QA deploy. Scans stay report-only as in P3.
- **Status:** Proposed; waiting for the user.

### D-24 · Original documents are retained in a restricted document store (user decision, 2026-10-07; replaces "hash only")
- **Context:** the first QA use showed that an officer cannot open what the applicant submitted. A reviewer has to see the documents before approving,
  and when extraction fails (as it does while P3's Bedrock quota is zero) a person must verify by eye. D-13 and the brief's hard rule ("documents are not
  persisted, hash only, as P3") made that impossible. The user chose option 1: an audited document store.
- **Decision:** originals go to a private, encrypted store: S3 in qa/prod (one bucket per environment, public access blocked, TLS-only policy, AES-256
  server-side encryption, lifecycle expiry as a backstop), a local folder in development. `DOCUMENT_STORE` is `s3` in qa and prod (enforced at startup).
- **What did not change:** bytes never enter the case state, the checkpoint, the audit log, logs, traces or prompts; the in-memory buffer still feeds `extract`;
  the applicant's file name is never kept; extracted values are still purged with the checkpoint.
- **Access:** officers only (a submitter gets 403); through `GET /cases/{id}/documents/{doc_ref}` or the officer page, never a public or pre-signed URL;
  the `document_viewed` audit row is written first (no audit, no document); the stored bytes are re-hashed against the SHA-256 recorded at upload and withheld
  with `document_integrity_failed` on a mismatch; a document is only reachable through its own case's state.
- **Safe display:** type sniffed from the bytes; png, jpeg, webp, gif, pdf and plain text are shown inline, everything else is a download; `nosniff`, `no-store`,
  `X-Frame-Options: DENY` and a restrictive CSP (`sandbox` except for PDFs, which a sandbox would break) on every document response.
- **Retention:** deleted with the case's checkpoints by `purge_checkpoints` (default 30 days after the case closes; the purge keeps the checkpoint if the delete fails, so
  it retries), and by the bucket lifecycle (default 90 days). **Degrade:** if the store is down at upload, the case continues with `documents_not_retained` and the
  officer is told; it is not blocked (a stricter bank would refuse the upload).
- **Consequences:** the hard rule and README privacy text changed; Terraform adds a bucket, its policies and S3 permissions on the API role per environment (the API role
  is now always created, and the existing one is kept with a `moved` block); the pipeline fills the bucket name in at deploy time because it contains the account id; CONTROLS
  gets C-18. Not decided here: a UAE-region bucket, KMS customer keys, malware scanning and legal-hold rules for real use.
- **Status:** Accepted (user, 2026-10-07).
