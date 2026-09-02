# Plan: P2 — corpus-ops 接到当前 main

**Goal:** 操作者在 workspace 里能把原始字节进 object store、ingest 可审计、OCR 写回冻结单元、库超出 frozen-12、redline/表/三年表可 cite、真 filing 有 in-app 通知——全部长在今天的 `main` 上，不整包 rebase 旧 PR。
**Approach:** 入口是 P1 已完成。Spec 已落在本 worktree：`specs/corpus-ops-depth.md`。旧 that pull request–#185 只作对照。不新开第二套 Docling 切分；OCR 扩 S1 已有 `ocr_applied`。串行：存储与状态机 → OCR → 结构 locator → 扩库 → 分析 → 监控。
**Spec link:** `specs/corpus-ops-depth.md`
**Queue:** 三份之二。入口 [`plans/2026-09-02-product-queue.md`](2026-09-02-product-queue.md)
**Tracker:** FALLBACK — 同 product-queue 头。
**Per-ticket loop (all bullets):** tdd → `uv run pytest -q`（无 live SEC / 无 live memory）；触及 web 时 `npx tsc --noEmit` → verification-before-completion → one commit on `docs/2026-09-02-product-queue`

## ACR（自 `specs/corpus-ops-depth.md` 原样粘贴）

bounded-context-guardian: yes — new work stays in `corpus` / `host` (tools + workspace notice) / `web`; object-store client is a corpus adapter, not a `controllers/` layer; ranking does not move into host; no second parse/chunk authority beside corpus-retrieval.
defensive-contract-validator: yes — Boundaries name empty / negative / overflow / concurrent / exception and bind them to SC 2–9.
error-handling-enforcer: yes — inherit typed EXITs; new paths named (object store down, illegal ingest transition, OCR failure, monitor degraded); no silent empty catch; no silent byte loss.
complexity-anti-drift: yes — five foci, one concern each; redline/table/statements are host tools or corpus writers, not nested in `model_node`.
minimal-change-verifier: yes — one logical program, sequenced as plan P2 tracer bullets on current main; parked PRs are not the merge vehicle.

## Harvest

**已决：** 假设门 1–12；ADR 0043；CONTEXT **object store**；七格式解析与父子分块已在 corpus-retrieval。
**开放：** MinIO vs 其它 S3 兼容、env 名、bucket 布局、OCR 引擎包、redline DOM、monitor 用 poll 还是 webhook。
**不做：** 合 the tracked change through the tracked change 当落地方式；改 §B1–§B6；P3 规模与消融。

## Tasks (ordered by dependency)

1. **Object store + ingest 指针** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC2；ADR 0043：原始字节进 object store；PG 元数据+指针；cite 树仍 extracted。
   - **Surface:** `corpus`
   - **Acceptance:** fixture ingest 在配置了 fake/minio 时把 raw bytes 存进 store 且 `read_file`/catalog 仍解析 locator；未配置时 typed 降级或文档化本地回退，无静默丢字节；`uv run pytest -q` exits 0。
   - **Completion:** 客户端库与 env 名由 implementer 选。对照 diff：parked #176。
   - Status: [x] complete
   - **默认第一刀**

2. **Ingest 状态机 + audit** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC3：文档化状态机；非法迁移 typed；audit 行可观察。
   - **Surface:** `corpus`
   - **Acceptance:** happy path 到 Ready 等价态；非法迁移/失败步骤 typed + audit；`uv run pytest -q` exits 0。
   - **Completion:** 状态枚举名与存储由 implementer 选。对照：parked the tracked change。
   - Status: [x] complete
   - [blocks: T1]

3. **选择性 OCR 写回冻结单元** — tag: `[implementation]`
   - **Inherits:** spec Focus 1 / SC4；S1 `ocr_applied` / `ocr_required_unavailable`；vision 非事实源。
   - **Surface:** `corpus`（既有 OCR 入口，不新开产品）
   - **Acceptance:** fixture 页 collapsed+image OCR 后 verifier 能匹配写回 span；引擎缺失走既有具名失败，不以空文本成功；无 vision 路径；`uv run pytest -q` exits 0。
   - **Completion:** 引擎选型开放。对照：parked #174。
   - Status: [x] complete
   - [blocks: T2]

4. **结构可引用单元（不换切分权威）** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 / SC5；corpus-retrieval SC11 子串关系；未知 filter → `{filings: []}`。
   - **Surface:** `corpus` + host `list_files`
   - **Acceptance:** 多 section fixture 产出稳定 locator 的 parent/child 或 section 单元；子串违规数 = 0；`list_files` 负例空列表；`uv run pytest -q` exits 0。
   - **Completion:** 字段名开放。对照：parked #181，禁止引入第二套滑动窗口。
   - Status: [ ] pending
   - [blocks: T3]

5. **扩 SEC 形图书馆（非 frozen-12）** — tag: `[implementation]`
   - **Inherits:** spec Focus 3 / SC6。默认 pytest 不打 live SEC。
   - **Surface:** `corpus` + web Corpus 首页
   - **Acceptance:** 额外 issuer/form 的 fixture 出现在 `GET /api/corpus`/卡片；默认 pytest 无 live SEC；`uv run pytest -q` exits 0；本弹若改 web 则 `npx tsc --noEmit` exits 0。
   - **Completion:** 扩库目录布局开放。对照：parked #182。
   - Status: [ ] pending
   - [blocks: T4]

6. **分析深度：redline + 表 + 三年表** — tag: `[implementation]`
   - **Inherits:** spec Focus 4 / SC7–SC8；不重做 `compare_facts` 网格；编造数字不得 `done`。
   - **Surface:** `host` tools + workspace GET + web thread
   - **Acceptance:** scripted 周转 redline 后 GET 含结构化结果；表绑定 locator；三年表来自 XBRL facts；`uv run pytest -q` exits 0。
   - **Completion:** 一个或三个工具、UI 形态开放。对照：parked #183。
   - Status: [ ] pending
   - [blocks: T5]

7. **真 filing monitor + in-app notice** — tag: `[implementation]`
   - **Inherits:** spec Focus 5 / SC9；既有 watchlist；不发邮件/Slack。
   - **Surface:** `host` workspace + web
   - **Acceptance:** 脚本源触发 notice 且 reload 仍在；源 down typed degraded 非空白 500；`uv run pytest -q` exits 0。
   - **Completion:** poll vs webhook 开放。对照：parked #184。
   - Status: [ ] pending
   - [blocks: T5]

## 不在本计划范围

- P1 真实检索进程（应已完成）
- P3 §B1 pdf 规模与 live 消融
- 把 #185 整包 rebase 进本分支

## End of round

P2 七弹落地后对照 `specs/corpus-ops-depth.md` SC1–SC10 做 verification-before-completion，再开 P3。
