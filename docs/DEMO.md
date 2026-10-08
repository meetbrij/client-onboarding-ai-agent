# Two-minute demo script

For a live walk-through or a screen recording. Everything is synthetic and marked SPECIMEN. No clip has been recorded yet; this is the script.

**Setup (before you start):** QA or the local compose stack (`docker compose up -d --build`, UI at `http://localhost:8000/ui`). Three sign-ins: submitter, officer-1, officer-2.
For a deployed demo with real extraction, the environment needs P3's KYC API key and an Anthropic key (see KNOWN_LIMITATIONS); locally, the fake KYC service and fake LLM are enough.

| Time | Do | Say |
|---|---|---|
| 0:00 | Slide or README diagram: intake, extract, screen, assess, approve, execute, audit | "A bank-style onboarding workflow. The model helps; it never decides. Sanctions matching, risk rating and the recommendation are plain Python. A person approves." |
| 0:15 | As the submitter, submit the `near_miss_dob_mismatch` case (`scripts/demo_submit.py near_miss_dob_mismatch`). Point at the busy state | "The submitter sees status only: no screening result, no risk rating, so nobody can be tipped off." |
| 0:35 | Sign in as officer-1, open the case. Show the screening hit: name score, DOB and nationality disagree | "A fuzzy hit on a listed name. Different date of birth and nationality, so it is classified 'possible', not 'strong'. The model's note is advisory and cannot clear it." |
| 0:55 | Open an original document from the case page | "Officers can open the originals; every view is written to the audit log first." |
| 1:10 | Try to approve without clearing the hit: the guard refuses. Clear the hit with a note, approve | "The guard is deterministic: no approval with an unresolved hit. Clearing needs a written reason. The same person cannot approve a case they submitted." |
| 1:30 | Show the customer created at the mock bank; show the audit trail and the chain check (`python -m onboarding.audit verify`) | "Every step is a hash-chained, append-only row enforced in Postgres. Retrying the bank call cannot create a second customer." |
| 1:45 | Show `evals/results/2026-10-08-live.json` or the README table | "Twelve synthetic cases through the live extraction service: 10 pass. The two misses are because the model read my blurred test documents better than I expected. I report that, I did not change the test." |
| 2:00 | Stop | |

Questions to be ready for: what the model is allowed to do (four advisory roles, `docs/MODEL_INVENTORY.md`); why the Anthropic API today (Bedrock quota is zero; synthetic data only; Bedrock is a configuration switch);
what is not done (`docs/KNOWN_LIMITATIONS.md`).
