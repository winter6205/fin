# 0023. Post-v1 ships one research-thread slice, not another one-shot surface

Date: 2026-08-22
Status: accepted

ADR 0013 made the CLI the first ship binary for v1; that one-shot CLI and `POST /ask` slice is already shipped. The next increment is a **research thread** the operator can actually talk in — follow-ups, citations, one contract across every surface that ships. Do not sequence another CLI-only or `/ask`-only milestone, and do not split the increment into T7/T9-style one-shot tasks: a shippable increment must still be a conversation. ADR 0013's "same compiled graph" rule still stands.
