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
