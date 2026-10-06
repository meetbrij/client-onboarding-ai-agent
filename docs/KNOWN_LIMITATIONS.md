# Known limitations

Accepted gaps, kept honest. Add an entry whenever a decision knowingly leaves something out.

- **Screening scope:** UN Consolidated List individuals only (entities are not indexed; OFAC is not included). The list's terms
  of use were not reviewed when the snapshot was vendored.
- **Matching:** Latin-script `token_sort_ratio` on normalised names. Original-script (Arabic, Cyrillic) names are not matched,
  and transliteration variants beyond what the aliases cover can be missed or over-matched. Demo-scale, not a screening vendor.
- **FATF reference data:** the call-for-action list is confirmed from two pages quoting the 19 June 2026 statement; the
  increased-monitoring list comes from one secondary source. FATF's own site could not be fetched. The October 2026 plenary may have
  changed both. See the header of `data/reference/jurisdictions.yaml`.
- **Occupation list:** illustrative internal policy, not a regulatory list.
- **Fixtures:** the true-hit cases echo real UN-listed names and listed DOB/nationality (DECISIONS D-18).
- **Pipeline copy** will drift from P3's (DECISIONS D-04).
- **Audit log:** a database superuser can drop the append-only trigger; tail truncation is undetectable without an external anchor
  of the head hash (DECISIONS D-02).
- **Fake KYC service** is a test double driven by recorded responses; it says nothing about real extraction quality.
