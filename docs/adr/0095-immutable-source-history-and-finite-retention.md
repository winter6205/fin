# 0095. Source history is immutable evidence with finite retention

Date: 2026-10-03
Status: accepted

Structured corpus knowledge needs stable historical attribution while allowing source corrections, model changes, and bounded storage reclamation. iknow retains immutable source versions, extraction records, and occurrence supports, and atomically publishes a mutable `(source_version, extraction_revision)` pointer with its compiled current view; replacement retires only the replaced supports, preserves other active origins, and never applies an implicit newest-wins truth rule. Historical evidence references have fixed, nonrenewable expiry, current dependencies protect live evidence, and guarded collection removes eligible retired payloads; an expired or purged citation remains explicitly unavailable instead of resolving to replacement content.

The trade-off is additional provenance and lifecycle bookkeeping in exchange for traceable old answers and recoverable updates. Ordinary updates preserve entity identity; same-source re-extraction replaces the accepted occurrence set without making older extraction links permanently active. The three contracts own independently reviewable details: [query](../../specs/structured-corpus-query.md), [evidence](../../specs/structured-corpus-evidence.md), and [maintenance](../../specs/structured-corpus-maintenance.md). This decision refines ADR 0094 and leaves ADRs 0091–0093 and existing runtime behavior unchanged until a separately verified implementation.
