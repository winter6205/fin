# 0060. One window tree: extract unit is the parent; density sizes in-window search keys

Date: 2026-09-17
Status: accepted

Company-corpus text is one tree, not two splitters. The **抽图窗** (ADR 0053) is the parent: extract, window embeddings, B/C provenance, and the citable body after a hit all use that id. **父子** children exist only inside a window as lexical search keys; **场景适配颗粒度** (density × type × section) packs those keys (and overflow slices under the 1200-token cap) and must not shrink the extract window to child size. Entering the graph does not choose a second RAG chunk strategy. This refines ADR 0059: A remains lexical-first, but BM25 hits children then returns the window. ADR 0055 still holds: children are not extract input; the four 档 stay separate.

Considered Options: two independent cuts (retrieve children vs extract windows); GraphRAG-style windows only with no children (duller clause/row lookup).
