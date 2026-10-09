# Plan: enterprise-kb-agent

**Goal:** 操作者只在 **workspace** 里把 fin 当企业知识问答入口用（**knowledge-QA** entry point，不是知识库；`docs/CONTEXT.md` **fin**、ADR 0090）：问得到带引用的答案、库能看能扩、问完能盯/导出/停、多家合成仍过 citation gate、每轮可审计且进程健康。
**Approach:** 粗颗粒度五波，对齐 spec 五个 Focus：Trust 与 Corpus 不互相阻塞；Grounded answers 可与它们并行；Workflow 的导出依赖对比工具；Fan-out 先接受 ADR 0034 再改 **host graph**。每波一条可演示竖切（API + UI 如需要 + 测试）。默认 `uv run pytest -q` 不碰 live SEC / live memory。BM25 保持归档（ADR 0032）。
**Spec link:** `specs/enterprise-kb-agent.md`
**Map:** [fin wayfinder: enterprise knowledge-base Agent](https://github.com/oocang/fin/issues/130)
**Tracker:** GitHub `oocang/fin` 主路径。[spec: enterprise-kb-agent](https://github.com/oocang/fin/issues/134)；T1 [#135](https://github.com/oocang/fin/issues/135)；T2 [#136](https://github.com/oocang/fin/issues/136)；T3 [#137](https://github.com/oocang/fin/issues/137)；T4 [#138](https://github.com/oocang/fin/issues/138) blocked by T3；T5 [#139](https://github.com/oocang/fin/issues/139)；T6 [#140](https://github.com/oocang/fin/issues/140) blocked by T5+T3。
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → typecheck+tests（`uv run pytest -q`；触及 Soft-Track 时加 `-m soft_track`；eval 弹加 `-m eval`；web：`npx tsc --noEmit`）→ verification-before-completion → one commit on the ticket branch
**End-of-round:** 五波都落地后对整轮 diff 做一次 code-review（Standards + Spec）；单弹 loop 不重复整轮 review。
**待写入:** empty — 术语均引用 CONTEXT / spec；ADR 0034 由 T5 从 proposed 标为 accepted，不新开词条。

## ACR（自 `specs/enterprise-kb-agent.md` 原样粘贴）

bounded-context-guardian: yes — new work stays in `corpus` / `retrieve` (port only) / `host` / `web`; watchlist and health are host/workspace capability, not a `controllers/` layer; retrieve ranking does not move into host.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 3–9.
error-handling-enforcer: yes — inherit existing typed EXITs; new paths named (watchlist empty vs error, export 404, fan-out overflow, `/health` degraded); no silent empty catch.
complexity-anti-drift: yes — five foci, one concern each; writing-plans must not dump all foci into one god-plan; fan-out is a graph join node, not nested in `model_node`.
minimal-change-verifier: yes — this spec is **one logical program**, not one commit; writing-plans sequences **one wave = one plan = tracer bullets**; conflict with “1 commit” is resolved by split, not by merging waves.

**OVERALL: PASS** — 本文件即 coarse 拆波；每波仍 1 commit。

## Harvest（已决 vs 开放）

**已决：**

- Workspace 是唯一产品入口；**citation verifier** 代码门；**retrieve port** + ADR 0032；**Soft-Track** 四 track 不重写。
- Typed EXITs：`retrieve_unavailable`、`unknown_locator`、`{chunks: []}` / `{filings: []}`、409 `{error: concurrent_run}`、503 `{error: ingest_missing}`。
- `/api/status` 保持 boot-scoped（ADR 0033）；`%` context window 不上该路由。
- `GET /health` 与 `/api/status` 分开（spec Focus 5）。
- Fan-out 拓扑：Send（或等价）后 join，再跑 citation verifier（ADR 0034 proposed → T5 accepted）。
- 默认 pytest 无 live SEC / live memory；9 gold 仍 `-m eval`。

**开放（implementer headroom）：**

- 对比工具的函数名、redline vs 表的 DOM 结构。
- Fan-out 上限 N 的具体整数（Acceptance 只要求「有 cap + 超限 typed overflow」）。
- Watchlist / notice / export / stop 的路由拼写与存储（Postgres 表 vs 侧文件），只要 reload 后仍在。
- `/health` JSON 字段名；eval 计数是 pytest plugin 还是 `scripts/` 一行命令。

## Tasks (ordered by dependency)

1. **Trust: evidence locators, health, eval counts** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 / SC8–SC10：terminal workspace **run** 把本轮 retrieve/read 的 locator 写入 `fin_run`（`evidence_locators`），follow-up 不继承上一轮 evidence；`GET /health` 在 `FIN_CHECKPOINT_URL` 未设时 typed degraded、URL 指向 down socket 时 typed degraded 且非空白 500；`-m eval` 仍 9 条且有文档化命令打印 done/refuse 整数。ADR 0033：不要把 health 做成 `/api/status` 的 per-run 遥测。
   - **Surface:** host（thread_store / workspace HTTP）+ tests/eval
   - **Acceptance:** GET 历史含 `evidence_locators`；follow-up 的本轮 evidence 不含上一轮 page；`GET /health` 两种 degraded 可观察；文档化命令对 `-m eval` 打出两个整数；`uv run pytest -q` exits 0。
   - **Completion:** 列名、health 探测实现、计数脚本位置由 implementer 选。
   - Status: [ ] pending — [#135](https://github.com/oocang/fin/issues/135)
   - [parallel]

2. **Corpus: form types on catalog and Corpus 首页** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 / SC3：`list_files` `form=10-K`|`8-K` 在仅 frozen-12 的 fixture 上 `{filings: []}`；测试语料含这些 form 时有行；`GET /api/corpus` / **Corpus 首页** 卡片带 form（或等价）。ingest 写入 extracted tree，默认 pytest 不打 SEC。
   - **Surface:** corpus + host catalog tool + web Corpus 首页
   - **Acceptance:** 负例空列表、正例有行、首页/API 能看见 form；`uv run pytest -q` exits 0；`web/` `tsc --noEmit` exits 0。
   - **Completion:** 是否扩展现有 `form` filter 键、卡片字段名由 implementer 选。
   - Status: [ ] pending — [#136](https://github.com/oocang/fin/issues/136)
   - [parallel]

3. **Grounded answers: comparison tools in the thread** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC2 / SC11：跨期/同业对比是 **host** 工具结果（表或 redline），GET `/api/threads/{id}` 上看得到结构化对比，不只散文；未经验证的数字不能 `done`；citation verifier 仍只填 verified spans；`refuse`/`step_cap` citations `[]`。
   - **Surface:** host tools + stream/turn table + web thread
   - **Acceptance:** scripted research 周转对比工具后 GET 含结构化对比；只编数字的答案非 `done`；既有 refuse/step_cap 空 citations 仍绿。
   - **Completion:** 一个工具还是两个、UI 是 table 还是 diff，由 implementer 选。
   - Status: [ ] pending — [#137](https://github.com/oocang/fin/issues/137)
   - [parallel]

4. **Workflow: watchlist, in-app notice, stop, export** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC4–SC6：watchlist CRUD，空列表 `[]`；Stop 结束 in-flight **run** 且不追加 HumanMessage，之后可再 send，并发仍 409 `{error: concurrent_run}`；从 thread 下载 citations 或 comparison 文件，缺 run → typed 404。Notice 仅工作台内、不外发邮件/Slack。
   - **Surface:** host workspace HTTP + web
   - **Acceptance:** watchlist 重启后仍在；Stop 与 409 可观察；export 有文件 body、缺 run 404；有 watched filing 进本地 catalog 时工作台能看到 notice。
   - **Completion:** 存储与路由拼写开放；export 绑定「本 thread 上一对比」只要 T3 已提供对比结果形状。
   - Status: [ ] pending — [#138](https://github.com/oocang/fin/issues/138)
   - [blocks: T3]

5. **Accept issuer fan-out topology (ADR 0034)** — tag: `[decision]`
   - **Inherits:** spec Focus 4；`docs/adr/0034-host-graph-issuer-fanout.md` 现 Status proposed：Send（或等价）后 join，再 **citation verifier**；Soft-Track classify 保持 flat。
   - **Surface:** `docs/adr/0034-host-graph-issuer-fanout.md`
   - **Acceptance:** 该 ADR Status 为 `accepted`；正文仍禁止无界并行、禁止无 join 的 per-issuer verifier、禁止在 fan-out 节点做 hybrid retrieve。
   - **Completion:** 是否补一句 cap 的推荐默认值由 implementer/决策作者选，不阻塞 Status 翻转。
   - Status: [ ] pending — [#139](https://github.com/oocang/fin/issues/139)
   - [parallel]

6. **Multi-issuer synthesize with cap** — tag: `[implementation]`
   - **Inherits:** spec Focus 4 / SC7；ADR 0034 accepted（T5）：一 **research thread** 对 capped issuers fan-out 再合成；每个贡献 issuer ≥1 citation；N+1 → typed overflow，不是 `done` 加静默丢弃。
   - **Surface:** host graph + workspace 转述
   - **Acceptance:** scripted 满 cap 合成带齐 citation；超 cap typed overflow；`uv run pytest -q` exits 0。
   - **Completion:** N 的数字、Send vs 其它 fan-out API 在 ADR 已约束形状下由 implementer 选。
   - Status: [ ] pending — [#140](https://github.com/oocang/fin/issues/140)
   - [blocks: T5, T3]
