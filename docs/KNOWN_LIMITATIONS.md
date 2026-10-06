# Known limitations

Accepted gaps, kept honest. Add an entry whenever a decision knowingly leaves something out.

- **Screening scope:** UN Consolidated List individuals only (entities are not indexed; OFAC is not included). The list's terms
  of use were not reviewed when the snapshot was vendored.
- **Matching:** Latin-script `token_sort_ratio` on normalised names. Original-script (Arabic, Cyrillic) names are not matched,
  and transliteration variants beyond what the aliases cover can be missed or over-matched. Demo-scale, not a screening vendor.
- **FATF reference data:** the call-for-action list is confirmed from two pages quoting the 19 June 2026 statement; the
  increased-monitoring list comes from one secondary source. FATF's own site could not be fetched. The October 2026 plenary may have
  changed both. See the header of `data/reference/jurisdictions.yaml`.
- **Screening recall on omitted names:** on the development set, variants that omit a middle name (for example 'Mohammad Noorani' for
  'Mohammad Aleem Noorani') score 66.7 to 84.2 and are not raised at the 85 threshold; the same threshold raised one false positive
  among 25 invented names (data/reference/screening_config.yaml has the numbers).
- **Occupation list:** illustrative internal policy, not a regulatory list.
- **Fixtures:** the true-hit cases echo real UN-listed names and listed DOB/nationality (DECISIONS D-18).
- **Pipeline copy** will drift from P3's (DECISIONS D-04).
- **Audit log:** a database superuser can drop the append-only trigger; tail truncation is undetectable without an external anchor
  of the head hash (DECISIONS D-02).
- **Fake KYC service** is a test double driven by recorded responses; it says nothing about real extraction quality.
- **Bedrock client untested against the real service:** `BedrockLlm` is exercised only with a stub client (throttling, retry, failure paths).
  Real calls wait for Bedrock quota (also the blocker for P3's KYC service).
- **Grounding checks are lexical:** an LLM summary or explanation is checked for rule ids that did not fire (and, for an explanation, for
  fired rules it fails to cite), and a draft for forbidden words and the wrong documents. They do not prove every sentence is true; that is
  why the officer page shows the rule outputs next to the wording.
- **Audit event guard is a key denylist:** it blocks obvious personal-data keys and long strings, not every possible leak; payload design stays
  a review item.
- **Offline runs use an in-memory audit log and checkpointer:** the append-only guarantee is a Postgres property, tested in `tests/test_audit_chain.py`.
- **Langfuse Cloud and `scripts/sync_prompts.py` are untested against the real service** (no project keys yet). Tracing and prompt fetching are tested with
  the real SDK and an in-memory exporter, and with stubs.
- **Tokens, not SSO:** per-person bearer tokens (hashed) and a signed session cookie stand in for the bank's identity provider (D-08). No rate limiting, no
  lockout, no token rotation workflow.
- **One recovery path per restart:** `recover()` runs at startup and can be called by any replica; there is no periodic sweep, so a case that fails while the
  service stays up waits for the next start (or for another officer action) to be retried. `last_error` shows the class of the failure.
- **Officer notes are stored in the audit log verbatim** (they are the rationale an auditor wants). They are free text and could contain personal data if an
  officer types it; guidance, not a technical control.
- **The UI was exercised in one browser** (and by HTTP-level tests), not across browsers or with assistive technology.
- **Extracted values live in the checkpoint until the retention purge** (default 30 days after the case closes); they are never in logs, audit rows, traces or prompts.
- **No metrics endpoint yet** (P3's Prometheus pattern); logs are JSON on stdout and traces go to Langfuse.
- **Phase 4 is not deployed.** Nothing has run on the cluster: the Terraform plans, the External Secrets sync, the IRSA and OIDC trust, the ALB and the DNS are
  unverified until the steps in `infra/terraform/README.md` are done. Local checks cover syntax, rendered manifests, the Postgres pod's security settings and the
  workflows' syntax only.
- **Scans only report (as in P3, `ENFORCE_SCANS: "false"`).** Local runs of the same tools: Trivy on the image found 44 HIGH and 0 CRITICAL, all Debian base-image
  packages (`python:3.12-slim`), most without a fixed version, none in Python dependencies; with enforcement on, that run would fail. Checkov on `k8s/`: 18 findings,
  of which `CKV_K8S_21` (default namespace) is an artefact of rendering the base without its overlay, `CKV_K8S_43` (image digest) conflicts with SHA-tag promotion as
  in P3, `CKV_K8S_35` (secrets as environment variables) is the External Secrets pattern as in P3, `CKV_K8S_14` is the `onboarding` placeholder the pipeline rewrites,
  `CKV_K8S_40` is Postgres running as its own uid 999 (the image needs it), and `CKV_K8S_15` asks for `imagePullPolicy: Always` on the Postgres image. Checkov on
  Terraform: 7 findings, all KMS customer keys and automatic rotation for Secrets Manager and ECR, which were left out to keep the stack thin. None is suppressed yet.
- **The Postgres image comes from Docker Hub** (`postgres:16.6`), is not built or scanned by our pipeline, and is subject to Docker Hub rate limits on a fresh node.
- **NetworkPolicies may not be enforced:** the manifests include them, but whether the cluster's CNI enforces them was not checked.
- **Coupled to P3's names:** the KYC service is reached at `nodejs-service.<qa|prod>.svc.cluster.local`, its Service name in P3's manifests. If P3 renames it, set `KYC_BASE_URL`.
- **One API replica and one Postgres pod per environment, no backups.** A restore story (snapshots of the EBS volume) is out of scope here; prod uses a Retain
  volume so data survives a deleted claim.
- **The first QA run will exercise untested paths:** ESO to Secrets Manager, IRSA for Bedrock (quota is zero, so the LLM falls back to templates after `LLM_RETRY_ATTEMPTS`),
  cross-namespace KYC calls (the real service will not read the smoke test's text files, so extraction is reported unavailable).
