# 0001. Python host agent first; retrieve via port, iknow-memory later

Date: 2026-08-21
Status: accepted

The interview gap is orchestration (citation, refuse, bounded retry), not another retrieval algorithm: iknow-memory already has stage-1 vector retrieve. This repo ships a standalone Python host agent first, depends only on a retrieve port, and uses an in-process retrieve adapter until an MCP client is worth the integration cost. Do not pause the agent to finish iknow-memory P0/P1, and do not compile-time couple the two repos.
