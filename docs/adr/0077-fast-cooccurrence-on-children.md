# 0077. Fast co-occurrence uses in-window children, not a second cut

Date: 2026-09-17
Status: accepted

**Fast 抽图** builds co-occurrence edges when noun-phrases share an **in-window child** (density packing under the parent **抽图窗**, ADR 0060 / 0063), not when they merely share a 1200-token window. Extract, citation, and retrieve still return the parent window. A second 50–100 token document cut is rejected. Standard LLM extract still runs per 抽图窗.

Considered Options: co-occur across the whole 1200-token window; a separate Fast-only token slide of the frozen unit.
