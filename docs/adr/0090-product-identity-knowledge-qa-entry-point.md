# 0090. Product identity: fin is a knowledge-QA entry point, not a knowledge base

Date: 2026-09-26
Status: accepted

## Context

`docs/CONTEXT.md` already settles fin's identity: **fin** (`:15`) is an enterprise knowledge-QA agent (知识问答助手) — the 入口 that answers from an ingested corpus through the **retrieve port** — and **知识库** (`:254`) is the process-wide corpus behind that port, whose `_Avoid_` list names "fin as a second 库". Financial filings are the current corpus domain, not the product: swap the corpus for legal or medical documents and the identity does not change.

Three tracked surfaces still asserted the opposite, and the strongest was self-referential: `specs/enterprise-kb-agent.md` carries the old framing in its own filename and identity banner, so a reader could reasonably conclude the spec outranks the glossary. The repository summary repeated it externally. Meanwhile `docs/CONTEXT.md:6` pointed collaborators to `docs/wayfinder/product-identity.md`, a disambiguation page inside a git-ignored directory — absent from every fresh clone, so the identity rule had no tracked home.

## Decision

fin's product identity is a **knowledge-QA entry point, not a knowledge base**, and this ADR is the tracked home for that ruling. It absorbs the ignored disambiguation page, including its confusable-term table. Product copy, the repository summary, and `README.md` describe fin accordingly.

## Considered options

- **Rename `specs/enterprise-kb-agent.md`** — rejected. The stem is the handle for wayfinder #130 and for merged PR history; renaming orphans inbound links in `README.md`, `plans/`, and `docs/CONTEXT.md` while changing no behavior. The filename is kept and its identity wording corrected.
- **Move the disambiguation page to a new tracked path** — rejected. It would create a third identity surface (glossary section, new page, this ADR) in permanent sync tension for one rule.
- **Un-ignore `docs/wayfinder/`** — rejected. That directory also holds per-session maps and tickets; tracking it would re-commit exactly what `ed186eb` ("keep session docs local; track product SSOT only") excluded.

## Confusable terms and their correct home

| Term | Correct meaning | Wrong reading |
| --- | --- | --- |
| `research thread` / `research-track` | Implementation term for the internal multi-track routing mechanism (`dialogue` / `clarify` / `off_topic` / `grounded`) | A "research-type product" or research persona |
| `档` | Search tier — the retrieval tier enum (`lexical` / `graph` / `global` / `sql`) | A product mode or user-facing mode picker |
| `资料` | Generic name for a corpus item (an ingested document/unit) | A "资料库 product" or a separate corpus product line |
| `抽图窗` / `父子分块` / `citation verifier` | Internal retrieval and citation mechanisms | Product features defining what fin "is" |
| `知识库` | The corpus of **资料** behind the **retrieve port**; the 库 is **iknow-memory** | fin itself, or a sidebar route |

These terms are real implementation vocabulary and remain valid. They describe internal mechanics, not fin's product category.

## Consequences

**Applied:** the naming rule that product copy must not call fin a research agent now has a tracked citation, which `docs/CONTEXT.md` links in place of the ignored page.

**Trade-offs:** the spec filename keeps a misleading stem indefinitely, so the correction depends on readers following its banner to this ADR. Identity now spans three documents (glossary term, spec banner, this ADR); the glossary remains the vocabulary SSOT and this ADR remains the *why*.

## Citing session-only documents

`docs/wayfinder/`, `docs/handoff/`, `docs/verify/`, `docs/research/`, `docs/design-references/`, `docs/cross-repo/`, and `plans/archive/` are git-ignored by design (`ed186eb`). A tracked file therefore must not cite them as reachable paths or as current authority. When such a citation goes, the substantive claim it carried stays in the citing sentence and is re-grounded in a tracked artifact — a spec, an ADR, or the glossary. Provenance already written in the past tense that needs no reachable target is left alone.
