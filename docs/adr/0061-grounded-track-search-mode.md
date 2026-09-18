# 0061. Track `grounded`; 检索档 is search mode; call-scoped injection

Date: 2026-09-17
Status: accepted

The Soft-Track leaf that binds corpus tools and the **citation verifier** is **`grounded`**, not `research` (ADR 0027 enum). Product copy still uses **research thread** for the conversation; fin is not a research-agent product. **检索档** A/B/C/D is **search mode** in English: it is not a Soft-Track value and not four host-graph retrieve nodes. Search-mode usage is call-scoped and written only in near context; checkpointed history is never rewritten when the mode changes. After **session compaction**, the host may re-inject the **search mode this turn needs**; a later different mode injects once again. Ranking stays behind the retrieve port. Wire alias `research` normalizes to `grounded`.

Considered Options: four LangGraph retrieve nodes per mode; mutating old SystemMessages when mode changes; naming the leaf `retrieve` (collides with the tool).
