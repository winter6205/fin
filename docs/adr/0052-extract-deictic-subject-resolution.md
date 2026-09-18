# 0052. Extract deictics bind to 资料主体, not a global name

Date: 2026-09-17
Status: accepted

Company-corpus **extract_triples** keeps existing canonical merge (normalized same name and type, then type and embedding gates; no LLM as the default merge judge). **指示语** such as 本公司 / the Company are not a global entity name. Each extract call is told the window’s **资料主体** (the 资料 this window came from: company name and optional ticker/CIK). If the model still emits a deictic and that 资料 has exactly one subject, rewrite to the subject name before merge. Gray zone (missing or conflicting subject, or deictic mixed with another issuer in the same window): a bounded model judge may see window text, section title, and 资料主体 fields — not the whole 资料 body. Unresolved deictics stay unmerged or a new node, never a world-wide 「本公司」.

Considered Options: merge-gates only (two issuers’ 「本公司」 collapse); last-resort model with the full 资料 as context (noise and cost); skip prompt injection and rewrite only after extract (more deictics, same merge bugs).
