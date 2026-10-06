# Evals

Status: **design only, and deferred until P3's KYC service is live (D-15, Phase 3b). No eval has been run; there are no numbers.**
When it runs, cases go through the live KYC service with SPECIMEN documents, so `kyc_response` below becomes a recorded
fallback for unit tests only, and extraction quality becomes part of what is measured end to end. README may quote only values from files in
`evals/results/`.

## What is being measured, and what is not
Measured: whether the workflow reaches the right recommendation, risk rating, sanctions hits and node sequence on 12
synthetic cases, and whether LLM-drafted client requests meet a rubric. **Not measured:** KYC extraction accuracy (P3's job;
its committed `app/eval/results/` is an all-failed run and must not be cited), real-world sanctions recall (12 cases are
illustrative, not statistical), fairness across name origins.

## Case format (`evals/cases/<id>.yaml`)
```yaml
id: near_miss_dob_mismatch
specimen: true                      # every fixture must say SPECIMEN; a test enforces it
applicant: {name: ..., aliases: [], dob: ..., nationality: ..., residence_country: ..., occupation: ...}
documents: [{doc_type: id_document, ...}]            # no real files; metadata only
kyc_response: {...}                 # recorded, shaped like P3's DocumentOut (fields[].confidence/needs_review/reason)
kyc_mode: ok | unavailable          # unavailable => the fake KYC returns HTTP 502/timeout
llm_mode: ok | unavailable
human_script: [{action: approve|reject|request_more_info, dispositions: {<entry_id>: cleared|confirmed}, note: ...}]
expected:
  recommendation: manual_review     # approve | reject | request_info | manual_review
  risk_rating: medium               # low | medium | high
  hits: [{entry_id: ..., class: possible}]            # [] when none
  fired_rules: [R-SAN-02]           # optional, checked as a set
  final_status: approved            # approved | rejected | awaiting_documents
  degraded: []
  trajectory: [intake, extract, screen, assess, approve, execute]
  required_steps: [screen, assess, approve]           # subsequence that must appear in order
```
Hit expectations reference UN entry ids from the committed snapshot; synthetic applicants are fuzzy variants of public list
names and are labelled `SPECIMEN`. A "true hit" case necessarily echoes a real listed person's name and listed DOB and
nationality; the applicant is otherwise fictional (see DECISIONS for how this tension with "no real people" is handled).

## The 12 cases
Recommendation precedence (Python): strong hit gives `reject`; else missing documents give `request_info`; else any needs-review
field, possible hit, extraction unavailable or rating medium/high gives `manual_review`; else `approve`.

| # | id | Covers | Rating | Recommendation | Hits | Scripted human | Final status |
|---|---|---|---|---|---|---|---|
| 1 | clean_approve | clean approve | low | approve | none | approve | approved |
| 2 | true_sanctions_hit | true hit (name, DOB, nationality agree) | high | reject | 1 strong | confirm hit, reject | rejected |
| 3 | near_miss_dob_mismatch | fuzzy near-miss, DOB mismatch | medium | manual_review | 1 possible | clear hit, approve | approved |
| 4 | missing_poa | missing document, then arrives | medium | request_info | none | request_more_info; docs arrive; approve | approved |
| 5 | low_confidence_extraction | low-confidence fields | medium | manual_review | none | approve | approved |
| 6 | high_risk_jurisdiction | residence in listed jurisdiction | high | manual_review | none | approve with EDD note | approved |
| 7 | high_risk_occupation | listed occupation | medium | manual_review | none | approve | approved |
| 8 | multiple_issues | missing doc + increased-monitoring jurisdiction + low confidence | high | request_info | none | reject | rejected |
| 9 | kyc_unavailable | KYC down: degrade | high | manual_review | none (screen runs on declared details) | request_more_info; stop | awaiting_documents |
| 10 | llm_unavailable | LLM down: template fallback | low | approve | none | approve | approved |
| 11 | alias_hit | applicant alias matches list alias | high | reject | 1 strong | confirm, reject | rejected |
| 12 | shared_given_name_no_hit | similar tokens, not a hit (precision) | low | approve | none | approve | approved |

Trajectories are written per case from the audit log's node events. Examples: case 1 `intake, extract, screen, assess,
approve, execute`; case 2 `intake, extract, screen, assess, approve` (ends); case 4 `intake, extract, screen, assess, approve,
await_docs, intake, extract, screen, assess, approve, execute`; case 9 `intake, extract, screen, assess, approve, await_docs`.
Cases 4 and 9 also cover the request-more-info loop. KYC-down is also asserted to produce `degraded: [extraction_unavailable]`.
Row 8's recommendation follows the precedence rule above (missing docs outrank manual review). For multi-pass cases (4, 9) the `expected` recommendation, rating, hits and rules describe the **first** approval pass. Case 8 has no occupation rule: its high rating comes from three medium rules (R-DOC-01, R-DOC-02, R-JUR-02), which needs the escalation rule in DECISIONS D-19. The fixtures are in `evals/cases/` and are checked against the vendored sanctions snapshot by `tests/test_fixtures.py`.

## Metrics
| Metric | Definition | Needs LLM? | CI gate |
|---|---|---|---|
| Recommendation accuracy | `recommendation.action == expected` / 12 | no | yes |
| Risk-rating accuracy | `risk.rating == expected` / 12 | no | yes |
| Final-status accuracy | final case status after the scripted human == expected / 12 | no | yes |
| Sanctions recall / precision | over (case, entry_id) pairs: raised vs expected hits; also reported for `strong` only | no | yes |
| Fired-rules set match | exact set equality where `fired_rules` is specified | no | yes |
| Trajectory exact match | audit-derived node sequence == expected | no | yes |
| Required-steps-present | `required_steps` is an ordered subsequence of the actual one | no | yes |
| Degrade correctness | `degraded` flags equal expected | no | yes |
| Invariant checks | no LLM-sourced field read by routing; no case reached `execute` without a human decision; audit chain verifies after each case | no | yes |
| Draft deterministic checks | draft names every missing doc, none extra, no forbidden terms (sanction, screening, risk, hit, approve) | no | yes |
| Draft quality (judge) | rubric below, reported separately, never mixed into the above | yes | no |

Thresholds live in `evals/thresholds.yaml`. The deterministic metrics are expected to be 100% because inputs are fixed; any
drop is a regression, so the gate is exact rather than "above 90%". Do not loosen a threshold to make a run pass without a
DECISIONS entry.

## Missing-document draft rubric (LLM-as-judge)
Applies to cases 4, 8 and any other case that produces a draft (n is small; report counts, not just percentages). Judge sees the
case's missing-doc list and the draft, not the sanctions result. Score each 0 (fail), 1 (partial), 2 (pass):
1. Requests exactly the missing documents, no others.
2. Does not disclose or hint at screening results, risk rating or any internal assessment (tipping-off risk).
3. Does not state or imply a decision (approved, rejected, will be approved).
4. Clear, polite, actionable (what to send, how); no invented deadlines, fees or policies.
5. Contains no personal data beyond the applicant's name.
Judge model should differ from the drafting model (Haiku 4.5 drafts; Sonnet-class judges, if quota exists). The result file records
judge model, rubric version and the judge prompt version.

## Harness design
- `evals/run.py --deterministic`: fake LLM (fixed text per role), fake KYC server from `kyc_response`, in-memory or ephemeral
  Postgres, human script applied through the real resume path. No network. Used in CI and in `make test-evals`.
- `evals/run.py --live`: real Bedrock for LLM roles, Langfuse on, KYC still recorded (D-15); judge on unless `--no-judge`.
- Human step: the harness calls the same resume API with the scripted decision (so interrupt-bound resume is exercised).
- Trajectory comes from audit rows, so the eval also checks the audit trail, and node span names in Langfuse equal audit node
  names (one shared constant).
- **Bedrock quota design** (P3's account has Anthropic per-minute quotas at 0 until raised): sequential calls only; exponential
  backoff with jitter on `ThrottlingException`/429/5xx, honouring `Retry-After` if present; max attempts and a per-run token
  budget flag; `--resume <partial.json>` skips cases already done so a throttled run can be completed in pieces; model id from
  `BEDROCK_MODEL_ID` (accepts a cross-region inference profile id; do not switch from `in.` to `global.` silently, record the
  profile in the result); budget estimate is about 3 to 4 LLM calls per case, so roughly 40 calls plus about 4 judge calls.
  If throttling persists, the case is recorded `llm_mode: template (throttled)`; a run is never silently reported as live.
- Cases are written once, then frozen; tuning thresholds uses the separate dev set (D-06). Any change to a case after the first
  committed result needs a DECISIONS entry, to avoid tuning to the test.

## Result file (`evals/results/<date>.json`)
```json
{"date": "...", "git_sha": "...", "mode": "deterministic|live", "llm_mode": "fake|bedrock|template",
 "model_id": "...", "inference_profile": "...", "prompt_versions": {"onboarding-summarise-case": 3},
 "sanctions_snapshot": {"source": "UN", "date": "..."}, "n_cases": 12,
 "metrics": {...}, "cases": [{"id": ..., "pass": ..., "actual": {...}, "expected": {...},
 "langfuse_trace_id": "...", "tokens": {...}}], "langfuse_run": "..."}
```
Langfuse: dataset `onboarding-eval-v1` with one item per case; each run links the case trace to its item, and the run name is
`<date>-<git_sha>`. The result JSON stores the run name and trace ids so the two can be cross-checked.

## CI
- Pull request and push to `qa`: `--deterministic` after unit tests; the job fails on any metric below `thresholds.yaml`.
- The live run and the judge are manual (`workflow_dispatch` or local) because of cost and quota; their result files are committed
  by hand and flagged with `mode: live`.
- README numbers script (Phase 5) fails if a number in the README is not found in the latest result file.
