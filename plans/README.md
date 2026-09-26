# Plans

**进度：** 会话笔记只留在本地、不入库（`ed186eb`；引用规则见 [ADR 0090](../docs/adr/0090-product-identity-knowledge-qa-entry-point.md)「Citing session-only documents」）；契约以本树 `plans/` + `specs/` + `docs/CONTEXT.md` 为准。

**第一闭环（ingest + live retrieve + cite；iknow 对票 corpus-ready-retrieve）：** [`first-closed-loop.md`](first-closed-loop.md) · spec [`../specs/first-closed-loop.md`](../specs/first-closed-loop.md)

**总计划（阶段表，不再当实施队列）：** [`retrieval-upgrade.md`](retrieval-upgrade.md)

**已闭合（本树近期）：** [`workbench-sidebar-rail.md`](workbench-sidebar-rail.md)（that pull request）；[`workbench-chrome.md`](workbench-chrome.md)（that pull request）；[`corpus-serve-ingest.md`](corpus-serve-ingest.md)（that pull request）；[`session-compaction.md`](session-compaction.md)（that pull request）。MDP 见 [`memory-dialogue-platform.md`](memory-dialogue-platform.md)——T1–T4 / T7–T9 已闭合；T5/T6 按 ADR 0045 非入库必做波。

**仍开着的工作台 prompt（本程序）：** Wave A 已合入（the tracked pull request / the tracked change closed）。**下一刀：** [`host-prompt-assembly.md`](host-prompt-assembly.md)（Wave B / the tracked change）。

**仍开着的 retrieve 内部债（非跨仓主线）：** [`retrieve-maintainability-followup.md`](retrieve-maintainability-followup.md)、[`2026-09-01-pr225-followup-plan-a.md`](2026-09-01-pr225-followup-plan-a.md)

**已闭合的 P1–P3 / D0 / Plan C / stage1 队列：** 归档全文是会话本地文件、不入库（`ed186eb`）；仓内可指的只有入口 stub [`2026-09-02-product-queue.md`](2026-09-02-product-queue.md) 与各切片 spec。

**切分/推送 tracer（已勾完，契约仍回源 spec）：** [`corpus-retrieval.md`](corpus-retrieval.md)
