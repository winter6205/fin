# 0062. Search mode uses the Soft-Track decision cascade

Date: 2026-09-17
Status: accepted

**检索档** (search mode) is assigned **after** Soft-Track has already chosen **grounded**, with the **same cascade** as ADR 0027 `decide_track`: high-confidence **rules** first; **structured classify** only when ambiguous; **inherit** on short follow-ups that stay in the same retrieve job; if the model still cannot name A/B/C/D, **clarify or a typed fail** (CONTEXT **具名失败** / 档未开) — then retrieve, or return without inventing a 档. Residual policy must not silent-open **C 综述**. Clarify is not chat-only: the host emits a **structured option list** (interrupt), the **工作台** shows a choice window, the operator picks, then the run resumes and executes. An optional override may pin a 档 without opening that window; there is no required per-turn picker. This is not a second product router, not four host retrieve nodes (ADR 0061), not usage-prose self-routing inside the answering prompt, and not v1 plan/tool-approval HITL.

Considered Options: every turn a mode picker; always-on classify LLM with silent default B; C only when the operator names it (CONTEXT 2026-09-17, withdrawn); clarify as free-text chat with no choice window.
