# 0034. Host graph may fan-out issuers then join before the citation verifier

Date: 2026-08-27
Status: accepted

Focus 4 of `specs/enterprise-kb-agent.md` needs one **research thread** to cover a capped set of issuers without serializing every issuer inside `model_node`. Decision: the **host graph** may Send (or equivalent LangGraph fan-out) per issuer, join, then run the **citation verifier** once on the synthesized answer. Soft-Track classify stays flat; ranking stays behind the **retrieve port**. Rejected: unbounded parallel agents, verifier-per-issuer with no join, hybrid retrieve inside the fan-out nodes. Recommended default issuer cap: 5 (implementer may tune the number; over-cap lists must return the typed overflow, not a hang).
