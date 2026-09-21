# 0080. Soft-Track gray-zone classify is Jev Choice

Date: 2026-09-21
Status: accepted

On the product path, Soft-Track’s ambiguous tier (rules miss, and inherit/clarify would not already decide) is a TypeSafe **Jev** closed-set Choice over `dialogue | clarify | off_topic | grounded`. The MiniMax `FIN_MODEL` JSON classifier is removed from that tier — not kept as a fallback when the Jev key is missing or the call fails. Low confidence, missing key, or judge-down is `clarify`, not silent `grounded` and not a second text model. Default pytest injects a Fake Choice (same closed set); it does not call Jev and is not the product classifier. Landing this path **requires** a live Jev item (real `FIN_JEV_API_KEY`, assert a real Choice round-trip) in the plan and in verification; missing key blocks done — never a green Fake pass counted as live. This amends ADR 0027’s “reuse `FIN_MODEL` until cost/latency evidence” clause only. The agent-loop **v1 model** still writes answers. Ranking stays behind the retrieve port (ADR 0061).

Considered Options: keep MiniMax JSON as product classify; keep MiniMax as fallback when Jev is down; run MiniMax and Jev in parallel and clarify on disagreement.
