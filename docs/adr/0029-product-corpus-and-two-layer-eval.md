# 0029. Product corpus and two-layer evaluation

Date: 2026-08-25
Status: accepted

The historical private fixture set is no longer the product or evaluation SSOT (ADR 0004, superseded). The locator contract is recorded in ADR 0028. `fin-serve` reads the operator-selected product corpus from local storage. Contract evaluation uses fixture artifacts produced by the ingest CLI; these artifacts are not included in the public distribution and must be supplied by maintainers when running corpus-dependent checks. Ingest remains CLI-only, and the ask path does not fetch raw source files. Contract evaluation covers HTML-unit citation pass, unknown-locator refusal, catalog miss, and cross-period behavior; the nine v1 gold cases are not the ceiling (ADR 0012, superseded). Capability evaluation is opt-in or nightly against the configured product corpus and a live model.

Considered options were keeping the initial small fixture set as the permanent evaluation SSOT, evaluating against a live source at test time, and using a broad corpus as the ingest default. The first eval fixture contained four documents across two periods and was produced by the ingest CLI.
