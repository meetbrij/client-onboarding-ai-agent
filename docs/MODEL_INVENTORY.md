# Model inventory

Skeleton (Phase 1). Filled in as components land; complete by Phase 5. One row per AI model, and one per
non-AI decision component, so a reviewer can see exactly what decides what.

## AI models

| Field | Value |
|---|---|
| Model id | Claude Haiku 4.5 on Amazon Bedrock. The inference profile id is configuration (`BEDROCK_MODEL_ID`) and is recorded on every call. *Not yet chosen for this project: P3 uses the India-only `in.anthropic.claude-haiku-4-5-20251001-v1:0`.* |
| Provider | Anthropic, served by Amazon Web Services (Bedrock) |
| Region | `ap-south-1` today; UAE production would need a model and profile available in `me-central-1` (see CONTROLS.md) |
| Purpose | Four advisory roles only: summarise the case; explain a recommendation from rule outputs; draft a missing-document request; optionally annotate a fuzzy hit |
| Not used for | Sanctions matching, risk scoring, the recommendation action, hit disposition, any approval or routing decision |
| Prompts and versions | `onboarding-summarise-case`, `onboarding-explain-recommendation`, `onboarding-draft-missing-docs`, `onboarding-annotate-hit`. Checked-in fallback copies are in `prompts/` (version `local-fallback`); the Langfuse-managed versions arrive in Phase 3. The prompt name and version are written to every audit row. |
| Inputs | Rule outputs and hit reasons; no DOB, ID numbers or addresses |
| Owner | *to be named* |
| Evaluation results | *none: evals have not been run (see EVALS.md, deferred to Phase 3b)* |
| Known limits | May paraphrase imperfectly; output is advisory and checked deterministically where possible; throttled under low Bedrock quota, in which case templates are used |
| Risk classification | *to be assigned against the institution's scheme* |
| Kill switch | `LLM_ENABLED=false` |

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
| P3 KYC service (Claude on Bedrock behind it) | Extracts fields from documents | The document bytes, in memory only |
| Langfuse Cloud | Traces and prompt management | `case_id`, synthetic names, prompt text, token counts; no DOB, ID number or address |
