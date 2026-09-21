# 0082. Citation verifier stays a code substring gate

Date: 2026-09-21
Status: accepted

The grounded **citation verifier** remains a programmatic check: a supporting span must be a contiguous substring of this-run retrieve/`read_file` text (ADR 0046). This host Jev program does **not** add a second model Choice (`supports | contradicts | says_nothing`) after that pass. Closed-set Jev is for Soft-Track and 检索档 routing (ADR 0080 / 0081), where rules cannot name the leaf. Quote-in-evidence is already decidable in code.

Considered Options: Jev support pick after the substring pass (`specs/citation-judge-check.md`); LLM-as-judge as the only citation gate.
