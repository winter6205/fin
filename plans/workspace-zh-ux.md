# Plan: workspace-zh-ux

**Goal:** 操作者在中文主界面下能清楚区分 Corpus 加载 / 未 ingest / 已就绪，阅读分组折叠的 Sources，主动回到 Corpus 首页而不被 `fin.lastThread` 弹走，并使用美化后的 Thinking 开关。
**Approach:** 先落文案与语言约定（决策子弹），再按用户心智拆四条垂直切片：Corpus 三态首屏、Sources 分组折叠、T5 会话级 restore 细化、全局中文化 + Thinking 开关。不改 host graph / SSE 契约；citation gate 粒度不变，只改呈现。Soft Brutalism 视觉 token 不动。
**Spec link:** `specs/workspace.md`（本增量为其 UX 抛光，无新 spec 文件）
**Tracker:** GitHub `winter6205/fin` — parent the tracked change; T1 the tracked change · T2 the tracked change · T3 the tracked change · T4 the tracked change · T5 the tracked change
**ACR:** all-yes（web-only 抛光；无新 bounded context）

bounded-context-guardian: yes — 改动限于 `web/` SPA；corpus/retrieve/host graph 契约不变；无 `controllers/` / `services/` 等技术层目录。
defensive-contract-validator: yes — `localStorage` / `sessionStorage` 访问保留 try/catch；T5 细化不改变服务端 thread 语义；Sources 分组纯展示，不伪造 citation。
error-handling-enforcer: yes — 无新空 catch；服务端 `assistant_message` / `status_reason` 仍原样展示（英文技术句 + 中文壳标签）。
complexity-anti-drift: yes — Sources 分组留在 citation 展示层；restore 门闩与路由意图分离，不膨胀 `store` 为 god module；Thinking 开关为 Composer 局部 UI。
minimal-change-verifier: yes — 五条 tracer bullet，每条一 commit；无 lockfile / 后端依赖；`/ask` 与 workspace API 不变。

**Per-ticket loop (all bullets):** tdd → `web/` `tsc --noEmit` → code-review → verification-before-completion → one commit on the ticket branch

**End-of-round:** T5 合并后对整个 `web/` diff 做一次 code-review（非每 commit 重复）。

**待写入:** (empty — flushed 2026-08-24: **Corpus 首页** / **citation span** in `docs/CONTEXT.md`)

## Harvest（已决 vs 开放）

**已决（inherits）：**

- workspace spec：空 corpus 在 home + 空 thread 展示卡片；点击 = create thread + prefill，不 auto-send。
- workspace spec SC：citations = 正文内联 chip **且** turn 下 sources list（locator + quote）。
- workspace spec line 42：回访恢复上次 `/t/{id}`（id 仍存在时）。
- `corpusLoaded` 已区分 fetch 完成 vs 未 ingest（`web/src/store.ts`）。
- Thinking 区仅当 `thinking_text` 非空时渲染；`thinking_ms: 0` 不显示空区。
- 主语言中文；Corpus / ingest / Thinking / Sources / thread / 10-Q / Resume / step_cap 等专业词保留英文或中英并列。

**开放（由子弹闭合）：**

- T4：已决 — `sessionStorage` 门闩 `fin.corpusHome`（`web/src/sessionKeys.ts`），不是 `location.state.skipRestore`。

## UI 文案表（T1 SSOT，T2–T5 inherits）

| 键                         | 中文（术语保留英文）                                   |
| -------------------------- | ------------------------------------------------------ |
| `corpus.loading`           | 正在加载 Corpus…                                       |
| `corpus.loading.hint`      | 从服务读取已冻结的 12 份 10-Q 索引                     |
| `corpus.not_ingested`      | Corpus 尚未 ingest                                     |
| `corpus.not_ingested.hint` | 索引已就绪，但提取树为空。请运行 ingest 后再提问。     |
| `corpus.ready.title`       | Corpus（冻结 12）                                      |
| `corpus.cta.empty`         | 新建对话（Corpus 为空也可试）                          |
| `nav.corpus_home`          | Corpus 首页                                            |
| `sources.summary`          | Sources（出处）· {pages} 页 · {spans} spans            |
| `sources.gate_hint`        | citation gate 匹配片段，可能有半句重叠，不代表发送失败 |
| `thinking.toggle.on`       | Thinking（推理）                                       |
| `thinking.toggle.off_hint` | 本轮将跳过推理轨迹                                     |
| `thread.loading`           | 加载对话中…                                            |
| `thread.not_found`         | 对话不存在                                             |
| `status.done`              | 完成                                                   |
| `status.refuse`            | 未完成                                                 |
| `status.step_cap`          | 步数上限（step_cap）                                   |
| `composer.placeholder`     | 输入关于 10-Q 的问题…                                  |
| `composer.send`            | 发送                                                   |
| `composer.running`         | 运行中…                                                |
| `composer.hint`            | Enter 发送 · Shift+Enter 换行                          |

## Tasks (ordered by dependency)

1. **UI 文案与语言约定** — tag: `[decision]`
   - **Inherits:** 用户确认：界面主语言中文；专业名词与报错逻辑可英文；spec Soft Brutalism chrome 不变
   - **Surface:** `docs/CONTEXT.md` + 本 plan「UI 文案表」
   - **Acceptance:** CONTEXT 含 **Corpus 首页** 与 **citation span**；本 plan「UI 文案表」完整；无 `web/` 产品代码变更
   - Status: [x] done — CONTEXT 词条 + 本文案表；tracker the tracked change closed 2026-08-25

2. **Corpus 三态首屏** — tag: `[implementation]`
   - **Inherits:** spec 空 corpus bound + `GET /api/corpus` 冻结 12；T1 文案表：`corpusLoaded=false` →「正在加载 Corpus…」；`corpusLoaded=true && length===0` →「Corpus 尚未 ingest」+ 说明 ingest 非网络 fetch；`length>0` → 卡片网格
   - **Surface:** `web/`（home / corpus 展示）
   - **Acceptance:** 三种状态互斥、文案符合 T1；加载中显示 skeleton 卡片（≥3）而非仅虚线框；未 ingest CTA 为「新建对话（Corpus 为空也可试）」；`web/` `tsc --noEmit` 通过
   - Status: [x] done — merged PR (the tracked change)
   - [blocks: T1]

3. **Sources 按文档·页分组与默认折叠** — tag: `[implementation]`
   - **Inherits:** spec SC citations sources list；CONTEXT **citation span**；gate ≥32 字滑窗粒度不变（非 send 失败）
   - **Surface:** `web/`（citation / sources 展示）
   - **Acceptance:** sources 默认展示汇总行「Sources（出处）· {去重页数} 页 · {条数} spans」；按 `document_id` → `page` 二级折叠；总 span 数 >10 时默认折叠；展开后可见完整 quote；有简短消歧（citation gate 匹配片段，可有半句重叠）；正文内联 chip 行为不变；`tsc --noEmit` 通过
   - Status: [x] done — merged PR (the tracked change)
   - [parallel]
   - [blocks: T1]

4. **T5 会话级 lastThread 与 Corpus 首页入口** — tag: `[implementation]`
   - **Inherits:** spec line 42 回访恢复；CONTEXT **Corpus 首页**；修正 per-mount 反复 bounce
   - **Surface:** `web/`（路由 / 侧栏 / home restore）
   - **Acceptance:** 冷启动（新 browser tab / session）且 `fin.lastThread` 有效时仍自动 `replace` 到 `/t/{id}`；同 session 内操作者经侧栏「Corpus 首页」进入 `/` 时不再被 restore 弹走；侧栏存在显式 Corpus 首页入口；`tsc --noEmit` 通过
   - Status: [x] done — merged PR (the tracked change)
   - [blocks: T1]

5. **全局中文化与 Thinking 开关美化** — tag: `[implementation]`
   - **Inherits:** T1 文案表；spec Thinking 全局偏好 off → `thinking.type=disabled`；无 `thinking_text` 时不预留 Thinking 区
   - **Surface:** `web/`（sidebar、thread、composer、dialog、404）
   - **Acceptance:** 操作壳层（侧栏、对话页、Composer、删除确认、404、状态标签 done/refuse/step_cap、Resume 横幅）为中文，术语按 T1 保留英文；Composer 的 Thinking 由 checkbox 改为符合 Soft Brutalism 的 toggle（1px 黑边、`#2563EB` accent、关/开状态可辨）；关时副文案「本轮将跳过推理轨迹」；`tsc --noEmit` 通过
   - Status: [x] done — merged PR (the tracked change)
   - [blocks: T2, T3, T4]

**Shipped:** 2026-08-23 on `main` via that pull request. Tracker the tracked change through the tracked change closed 2026-08-25. Do not reopen these bullets.
