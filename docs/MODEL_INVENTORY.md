# Model inventory

Updated 2026-10-08 (Phase 5). One row per AI model, and one per
non-AI decision component, so a reviewer can see exactly what decides what.

## AI models

| Field | Value |
|---|---|
| Model id | Claude Haiku 4.5. **Today:** `claude-haiku-4-5` through the Anthropic API (`LLM_BACKEND=anthropic`), because the Bedrock quota in the account is zero. **Target:** Amazon Bedrock (`LLM_BACKEND=bedrock`, `BEDROCK_MODEL_ID`, P3 uses the India-only `in.anthropic.claude-haiku-4-5-20251001-v1:0`). The model id is recorded on every call. |
| Provider | Anthropic (API, demo and evals; synthetic data only) or Anthropic served by AWS Bedrock (target) |
| Region | `ap-south-1` today; UAE production would need a model and profile available in `me-central-1` (see CONTROLS.md) |
| Purpose | Four advisory roles only: summarise the case; explain a recommendation from rule outputs; draft a missing-document request; optionally annotate a fuzzy hit |
| Not used for | Sanctions matching, risk scoring, the recommendation action, hit disposition, any approval or routing decision |
| Prompts and versions | `onboarding-summarise-case`, `onboarding-explain-recommendation`, `onboarding-draft-missing-docs`, `onboarding-annotate-hit`. Checked-in fallback copies are in `prompts/` (version `local-fallback`); the Langfuse-managed versions arrive in Phase 3. The prompt name and version are written to every audit row. |
| Inputs | Rule outputs and hit reasons; no DOB, ID numbers or addresses |
| Owner | *to be named by the institution; the project owner is the repository owner* |
| Evaluation results | Live run 2026-10-08, 12 synthetic cases: 10 pass; the two misses are about the extraction inputs, not the LLM. LLM output checks (rule citation, draft contents) are deterministic. Judge (claude-sonnet-5-5, rubric v1) scored the 2 missing-document drafts 10/10 each. See README "Evaluation" and `evals/results/2026-10-08-live.json`. Illustrative, not statistical. |
| Known limits | May paraphrase imperfectly; output is advisory and checked deterministically where possible; throttled or unavailable models fall back to templates (the `llm_unavailable` case shows this) |
| Risk classification | *to be assigned against the institution's scheme* |
| Kill switch | `LLM_ENABLED=false` (tested: the workflow runs unchanged with templates) |
| Output checks | Summary and explanation may not name a rule that did not fire (and an explanation must cite every fired rule); a draft must ask for exactly the missing documents and may not mention screening, risk or a decision. Failures fall back to templates and are audited as `llm_output_rejected` |
| Traceability | Each call writes an `llm_call` audit row (prompt name and version, model id, token counts) and a Langfuse generation |

## Non-AI decision components (listed for completeness)

| Component | Role | Spec |
|---|---|---|
| Sanctions scorer (rapidfuzz `token_sort_ratio`, raise at 85, strong at 92, plus DOB and nationality corroboration) | Raises hits and classifies them | `onboarding/screening/scorer.py`; thresholds and dev-set evidence in `data/reference/screening_config.yaml`; DECISIONS D-06 |
| Risk rule engine (8 rules, rating with escalation) | Rating and fired rules | `onboarding/rules/engine.py`; DECISIONS D-19 |
| Recommendation function | `approve / reject / request_info / manual_review` | `recommend_action`; DECISIONS D-14 |
| Approval guard | Blocks approval with undisposed hits or unavailable extraction | DECISIONS D-14 |

## Other third-party services in the data path

| Service | Role | Data sent |
|---|---|---|
| P3 KYC service (Claude Haiku 4.5; the Anthropic API today, Bedrock by configuration) | Extracts fields from documents | The document bytes, in memory only. With the Anthropic route they leave AWS: synthetic specimens only |
| Anthropic API (our advisory LLM, while Bedrock quota is zero) | The four advisory roles | Rule outputs and hit reasons only; no DOB, ID number or address; synthetic data only |
| Anthropic API (eval judge, `claude-sonnet-5-5`) | Scores missing-document drafts in live evals only | The missing-document list and the draft text |
| Langfuse Cloud | Traces and prompt management | `case_id`, synthetic names, prompt text, token counts; no DOB, ID number or address |
