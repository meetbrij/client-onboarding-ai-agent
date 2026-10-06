# Controls mapping

Maps this design to the CBUAE *Guidance Note on the Consumer Protection and Responsible Adoption and Use of Artificial
Intelligence and Machine Learning by Licensed Financial Institutions in the U.A.E.* (issued 23 February 2026 according to
secondary sources).

## Source status: read this first
- **The official text was not read.** The CBUAE Rulebook page
  (`rulebook.centralbank.ae/en/rulebook/guidance-note-consumer-protection-and-responsible-adoption-and-use-artificial-intelligence`)
  returned HTTP 403 to both the fetch tool and `curl` on the date of writing (2026-10-05).
- Two secondary summaries were fetched. They **disagree on structure**: one lists numbered sections 2 to 10 (governance, model
  inventory, bias testing, transparency, data and kill-switch, monitoring, human oversight, consumer rights, outsourcing);
  the other says the note has ten sections but does not reproduce numbers or headings. Neither is authoritative.
- Therefore this file uses **topic names only, no clause or section numbers**, and the "Guidance topic" column is paraphrase,
  not quotation. Before the README cites any of this, the user should download the PDF from the Rulebook in a browser,
  place it in `docs/sources/` (gitignored if licensing is unclear) and we replace topic labels with exact headings and numbers.
- The note is reported as non-binding guidance that signals supervisory expectations. It refers to the CBUAE Model Management
  Standards (not read), and uses a human oversight scale of in-the-loop / on-the-loop / out-of-the-loop (as described by the
  secondary sources).
- This is an engineering mapping, not legal advice or a compliance attestation.

## Controls table

Status: **Planned** = not built; **Built; tested** = the control exists and the named tests check it (they run in CI and locally); screenshots and the Langfuse and Bedrock evidence come in Phase 5.

| Guidance topic (paraphrased) | Control | How it is implemented | Evidence (file / test / screenshot) | Status |
|---|---|---|---|---|
| Human oversight (human in the loop for high-impact decisions) | C-01 No auto-approval path | `execute` is reachable only via `approve` with a human decision on the checkpoint; approval guard blocks approve while hits are undisposed or extraction was unavailable; graph test asserts no edge from `assess` to `execute`; no config flag disables the gate | `tests/test_service.py::test_the_graph_has_no_path_to_execute_except_through_approve`, `::test_the_execute_node_refuses_to_run_without_an_officers_approval`, `tests/test_routes_ignore_llm.py`; `app/onboarding/graph/build.py` | Built; tested |
| Human oversight | C-02 Attributable decisions, separation of duties | Officer bearer tokens (D-08); the officer id is the `actor` of the `decision_received` audit row; submitter cannot decide own case; stale/duplicate resumes dropped and audited | `tests/test_service.py` (separation of duties, stale and duplicate decisions, audit-before-effect), `tests/test_service_postgres.py` (concurrent decisions apply once), `tests/test_api.py`; `decision_received` audit rows carry the officer id and note | Built; tested |
| Human oversight | C-03 Officer can always stop or override | Officer may reject or request more info at any time; LLM output cannot clear a hit; only officer disposition can | `tests/test_decision.py` (guard), `tests/test_llm_never_decides.py` (hostile or absent LLM output changes nothing), `tests/test_nodes.py::test_screen_annotates_possible_hits_only_and_never_changes_the_hit` | Built; tested |
| Explainability / transparency | C-04 Every recommendation explains itself | `risk.fired_rules[]` (rule id, inputs, explanation) and each hit's reason record (list entry, source, snapshot date, algorithm, score, name/DOB/nationality agreement) are shown on the officer page and stored in the audit log; the LLM explanation only restates those outputs | `tests/test_rules.py`, `tests/test_scorer.py`, `tests/test_ui.py::test_officer_sees_the_assessment_with_advisory_labels`; the officer page and `GET /cases/{id}`; `rule_result` and `sanctions_hit` audit rows. Screenshots: Phase 5 | Built; tested |
| Explainability | C-05 Explanation is grounded | LLM prompts receive only rule outputs; a test checks that every rule id in the generated text exists in the fired list; template fallback when LLM is down | `tests/test_llm.py` (grounding checks, template fallback), `tests/test_nodes.py::test_assess_replaces_an_ungrounded_explanation_with_the_template` | Built; tested |
| Auditability / record keeping | C-06 Append-only, tamper-evident log | Postgres table, trigger rejecting UPDATE/DELETE/TRUNCATE, INSERT-only app role, SHA-256 chain (`prev_hash`, `row_hash`), `verify_audit_chain` command | `tests/test_audit_chain.py` (trigger, role grants, tamper detection, concurrent appends on Postgres, CLI exit codes); `python -m onboarding.audit verify` run against the compose database (OK, 20 rows) | Built; tested |
| Auditability | C-07 Model/prompt provenance on every call | Each LLM call writes prompt name + version, model id, token counts to audit and Langfuse | `tests/test_runner_fixtures.py::test_a_full_run_writes_the_expected_audit_event_types_with_prompt_versions`; `tests/test_langfuse_tracing.py` (generation carries prompt name and version). Langfuse-managed versions: needs a real Langfuse project | Built; Langfuse side untested against the real service |
| Governance and accountability | C-08 Documented decisions and limits | `DECISIONS.md`, ADRs, `KNOWN_LIMITATIONS.md`, owner named in the model inventory | `docs/DECISIONS.md` (D-01 to D-22), `docs/KNOWN_LIMITATIONS.md`; the owner in the inventory is still to be named | Partly done |
| Model inventory | C-09 Inventory of AI components | `docs/MODEL_INVENTORY.md`: model id, provider, purpose, prompts + versions, owner, eval results, limits, risk classification; the deterministic scorer and rule engine are also listed as non-AI decision components | `docs/MODEL_INVENTORY.md` (skeleton filled for the LLM roles and the deterministic components; evaluation results and owner pending) | Partly done |
| Monitoring / model validation | C-10 Measured behaviour before release | Eval harness (decision, trajectory, sanctions, judge) with a CI gate on deterministic metrics; results committed | `evals/results/<date>.json`; CI run | Planned (after Phase 3b) |
| Monitoring | C-11 Kill switch | `LLM_ENABLED=false` makes every LLM role fall back to templates without redeploy of code (config only); the workflow still runs since the LLM never decides | `tests/test_llm.py::test_disabled_service_never_calls_the_model_and_flags_it`, `Settings.llm_enabled`; a disabled or failing model is shown to the officer as degraded | Built; tested |
| Data quality and provenance | C-12 Provenance of screening data | Vendored list snapshot with manifest (source, date, sha256); snapshot date on every screening result; no runtime fetch in prod | `data/sanctions/MANIFEST.json` and `tests/test_sanctions_loader.py` (hash check on load); `tests/test_nodes.py::test_screen_records_hits_with_reasons_and_audit_rows` (snapshot date on every hit) | Built; tested |
| Data protection / privacy (UAE PDPL) | C-13 Data minimisation | Documents never persisted (SHA-256 only); no PII in logs, traces, prompts or audit payloads beyond `case_id` and synthetic names; redacting log filter and a test reading real log output; synthetic data only | `tests/test_prompt_payloads.py` (no DOB, ID number or address in prompts, audit rows or the approval payload), `tests/test_api.py::test_a_failed_run_stores_only_the_error_class_and_logs_no_personal_data`, `tests/test_service_postgres.py::test_checkpoints_hold_state_but_never_document_content`, `tests/test_langfuse_tracing.py::test_spans_carry_no_personal_data`. Extracted values do live in the checkpoint until the retention purge (D-13) | Built; tested |
| Third-party / outsourcing | C-14 Vendor accountability: Amazon Bedrock | Listed in inventory with data flow, region, what is sent (rule outputs, no PII), model version pinned in config, fallback template if unavailable; IAM least privilege via IRSA | `MODEL_INVENTORY.md`; Terraform IRSA policy | Planned |
| Third-party / outsourcing | C-15 Vendor accountability: Langfuse | Same treatment; Cloud for the portfolio build (D-07); documented in-region self-host path for a bank; payloads PII-free | `DECISIONS.md` D-07; `tests/test_prompt_payloads.py` | Planned |
| Third-party / outsourcing | C-16 Vendor update testing | Model id and prompt versions are pinned and recorded; changing either requires re-running evals (documented procedure) | `docs/EVALS.md` | Planned |
| Consumer rights (human review on request) | C-17 Human decides every case | Every case ends with a named human decision | `tests/test_runner_fixtures.py` (every fixture ends with a named officer's decision or waits for one); `execute` is unreachable otherwise | Built; tested |

## Known gaps against the guidance topics (be upfront)
- **Bilingual (Arabic/English) explanations:** reported by a secondary source as expected for customer-facing explanations.
  Our UI and explanations are English only, and the explanation is for the officer, not the customer. Gap.
- **Customer notification of AI use / opt-out:** no customer-facing AI interaction exists (the LLM drafts a request that a human
  sends). Not applicable by design, but a bank deploying this should state it in policy.
- **Bias and fairness testing:** no ML model makes decisions here. The fuzzy scorer can disadvantage transliterated Arabic and
  South Asian names (more false positives). Not tested beyond the eval cases; listed in KNOWN_LIMITATIONS.
- **Board-level governance, incident response, complaints handling, annual validation:** organisational controls, outside a
  code repo.
- **Officer identity** is a static-token stand-in for bank SSO (D-08).
- **Superuser can drop the audit trigger;** the hash chain detects later edits but not tail truncation unless the head hash is
  anchored externally (D-02).

## Data protection: UAE PDPL and the region question
- **Law:** UAE Federal Decree-Law No. 45 of 2021 on the Protection of Personal Data (PDPL). Whether a licensed financial
  institution is in scope, how it interacts with CBUAE rules, and cross-border transfer conditions are legal questions; this
  repo does not answer them. Counsel review is a prerequisite for any real use.
- **Today:** the platform runs in `ap-south-1`. P3 uses India-only Bedrock inference profiles (`in.` prefix: inference stays in
  `ap-south-1` and `ap-south-2`), and its CLAUDE.md forbids switching to a `global.` profile. All data here is synthetic.
- **Prod in `me-central-1` (UAE):**
  - Checked with the AWS regional-availability tool on 2026-10-05: **Amazon Bedrock the service is available in
    `me-central-1`**.
  - **Not verified:** whether Claude Haiku 4.5 is available there, in-region or via a geographic inference profile. AWS's
    model card page was truncated in the tool output. Verify with
    `aws bedrock list-foundation-models --region me-central-1` and `aws bedrock list-inference-profiles --region me-central-1`
    before promising anything. If the model is not offered there, residency means either choosing a different model that is, or
    accepting cross-region inference and documenting the destination regions.
  - What changes: a new cluster (P3's EKS, ECR, ACM, Secrets Manager and Route 53 records are all `ap-south-1`) or a region
    move of the platform stack; `AWS_REGION` and `BEDROCK_MODEL_ID` set per environment; IRSA policy ARNs for the new profile;
    ECR replication or a new repo; quota requests in the new region (separate quotas, so P3's quota blocker repeats there);
    Langfuse moved in-region (self-hosted) or a project in an acceptable region; KMS keys in-region; Terraform state bucket
    region. Application code does not change if region and model id are configuration, which is a design requirement here.
