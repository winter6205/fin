# 0062. Search mode uses the Soft-Track decision cascade

Date: 2026-09-17
Status: accepted

**检索档** (search mode) is assigned **after** Soft-Track has already chosen **grounded**, with the **same cascade** as ADR 0027 `decide_track`: high-confidence **rules** first; **Jev** Choice only when ambiguous (ADR 0081); **inherit** on short follow-ups that stay in the same retrieve job; if the cascade still cannot name a 档, **clarify or a typed fail** (CONTEXT **具名失败** / 档未开) — then retrieve, or return without inventing a 档. Residual policy must not silent-open **global** 综述 (formerly C). Clarify is not chat-only: the host emits a **structured option list** (interrupt), the **工作台** shows a choice window, the operator picks, then the run resumes and executes. An optional override may pin a 档 without opening that window; there is no required per-turn picker. This is not a second product router, not four host retrieve nodes (ADR 0061), not usage-prose self-routing inside the answering prompt, and not v1 plan/tool-approval HITL.

Amended 2026-09-21 (ADR 0081): “structured classify” on the product path is Jev Choice over `A | graph | global | D`, not MiniMax JSON; pytest Fake Choice; no MiniMax fallback. _Stage (landed): the 档 gray-zone Jev tier is implemented — rules → Jev Choice → inherit → clarify / typed fail, with the default pytest Fake Choice and a required live `jev_live` round-trip; no MiniMax JSON classify remains on the product path._

Considered Options: every turn a mode picker; always-on classify LLM with silent default B; C only when the operator names it (CONTEXT 2026-09-17, withdrawn); clarify as free-text chat with no choice window.
