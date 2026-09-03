# Agent prompts — product-queue P1 → P2 → P3

操作者按序开 **3 个智能体**（或同一对话连贴三段）。后一段的入口条件是前一段计划文件里全部 Status 已 `[x]`。

- fin worktree：`/home/winner/projects/fin-wt-product-queue` 分支 `docs/2026-09-02-product-queue`
- iknow 主仓：`/home/winner/projects/iknow-memory` 的 `main`（P1 需要改 spec/代码时在该仓提交，不要另开计划 worktree）
- 禁止：`gh issue create`；合 fin #172–#185 / #195；默认 `uv run pytest -q` 启真实 SEC 或真实 iknow
- 循环：每颗子弹 TDD → 实测 → **one commit**；整轮三份完才 code-review
- 中文汇报

下面三块各自从 `---` 到 `=== END P# ===` **整段复制**给对应智能体。

---

## 调度一句话

先贴 **PROMPT P1**。P1 回报五弹全绿后再贴 **PROMPT P2**。P2 回报 SC1–SC10 全绿后再贴 **PROMPT P3**。不要三份同时开。

---

### PROMPT P1

```
你是实施智能体。只做 P1，不要开 P2/P3，不要合 GitHub 上 fin #172–#185。

工作区
- 主：/home/winner/projects/fin-wt-product-queue ，分支 docs/2026-09-02-product-queue
- 对侧：/home/winner/projects/iknow-memory （P1 T1 若要改基线章节、T2–T5 若要改 push/retrieve/invalidation）
- 计划 SSOT：plans/2026-09-02-p1-live-retrieve-wire.md
- 契约：fin specs/corpus-retrieval.md §B2–§B4、SC15/18/21/23/24/25/39、§B6；iknow specs/retrieval-stack.md 同名「跨仓对齐基线」

已在 main（不要重做 Fake 版 T14–T21 / D0 字段映射）
- MCP push/invalidation 客户端、DEFAULT_MCP_COMMAND=("iknow-memory",)、chunks 信封
- 本计划只补：真实 iknow stdio 进程上的验收

执行顺序（一弹一 commit， Conventional Commits，提交在本 worktree 分支；iknow 改动在 iknow 仓自己的 commit）
1. 跨仓基线 sha256：uv run python -m fin.corpus.baseline <fin specs/corpus-retrieval.md> <iknow specs/retrieval-stack.md> 必须 exit 0。差 1 字节就同步章节，取段规则不许改。勾选计划 T1。
2. opt-in 真实进程：push 现有 child 块（不等 pdf≥20）→ retrieve 文本与推送逐字节相同；同一批再推 2 次块总数增量=0。默认 pytest 不跑。勾 T2。
3. opt-in：分批中断后续传（已成功批次不二写，补齐到预期总数）；进程杀掉后推送是具名失败不是空成功。勾 T3。
4. opt-in：同一旧块失效两次不报错，有效时刻 retrieve 不再命中旧文本。勾 T4。
5. opt-in：产品 compile 路径 retrieve 不含能力关闭键（ADR 0039 K2）；degraded=true 不得当 {chunks:[]} 空成功。勾 T5。

每弹：先写失败测试，再最小实现。默认 uv run pytest -q 必须仍绿且不启 iknow。汇报用中文：改了什么、命令与 exit、未验证项。不要 gh issue。不要 code-review 整轮（留给三份都完）。做完把 plans/2026-09-02-p1-live-retrieve-wire.md 五条 Status 打 [x]。
=== END P1 ===
```

---

### PROMPT P2

```
你是实施智能体。只做 P2。入口条件：plans/2026-09-02-p1-live-retrieve-wire.md 五条都是 [x]。若不是，停下来只报告缺口，不要开始 P2。

工作区
- /home/winner/projects/fin-wt-product-queue ，分支 docs/2026-09-02-product-queue
- 计划 SSOT：plans/2026-09-02-p2-corpus-ops-on-main.md
- 契约：specs/corpus-ops-depth.md（SC1–SC10）；ADR 0043；docs/CONTEXT.md **object store**
- 对照（只读，禁止 git merge 这些 PR）：fin #176 object store、#175 ingest state、#174 OCR、#181 structure units、#182 expand library、#183 analysis、#184 filing monitor

硬约束
- 接到当前 main 的 fin.corpus 解析/切分，禁止第二套 Docling/LlamaIndex 切分器
- OCR 扩展已有 ocr_applied / ocr_required_unavailable，禁止再做一套 OCR 产品
- 默认 pytest 无 live SEC、无 live iknow；改 web 则 npx tsc --noEmit
- 不要改 specs/corpus-retrieval.md §B1–§B6
- 不要做 P3（pdf≥20、live 消融）

串行子弹（一弹一 commit，勾选计划 Status）
1. Object store + ingest 指针（SC2）：fake/minio 时 raw bytes 进 store，catalog/read_file 仍解析 locator；未配置 typed 降级或文档化本地回退，禁止静默丢字节
2. Ingest 状态机 + audit（SC3）
3. 选择性 OCR 写回冻结单元（SC4），citation verifier 能匹配写回 span
4. 结构可引用单元（SC5）且 child ⊆ parent ⊆ frozen unit；list_files 未知 filter → {filings:[]}
5. 扩非 frozen-12 fixture 出现在 GET /api/corpus / 首页卡片（SC6）
6. redline + 表抽取 + XBRL 三年表（SC7–SC8）；编造数字不得 done；不重做 compare_facts
7. 脚本化 monitor 源 → reload 后 in-app notice；源 down typed degraded 非 500（SC9）

每弹 TDD。uv run pytest -q 保持绿。中文汇报。不要 gh issue。不要整轮 code-review。做完七条 Status [x] 并对照 SC1–SC10 列实测证据。
=== END P2 ===
```

---

### PROMPT P3

```
你是实施智能体。只做 P3。入口条件：P1、P2 计划文件全部 Status 为 [x]。否则停并报告。

工作区
- /home/winner/projects/fin-wt-product-queue ，分支 docs/2026-09-02-product-queue
- 计划 SSOT：plans/2026-09-02-p3-eval-scale.md
- 契约：specs/corpus-retrieval.md §B1、SC11–SC12、SC31–SC38
- 事实：data/corpus/dataset/manifest.json 里 pdf 计入规模仍是 12，known_gaps=[] 是不诚实的，必须先修

不要
- 把 corpus-retrieval.md 里 T22/T23 的 Fake/smoke 勾选当成完成
- 改 §B1 数字正文；合 #172–#185；修 A9 memory-dream；做 iknow 质量栈 T6+
- 默认 pytest 读 data/corpus 大语料或打 live SEC

串行子弹（一弹一 commit）
1. expand-primary：计入规模的 pdf 一级样本 ≥20，带来源与许可；不达标则 known_gaps 必须非空。默认 pytest 不因本弹触网失败。
2. 连续两次 dataset 重建 manifest sha256 一致；verify 不一致=0；chunk_total 仍在 20000–120000；-m eval 切分契约绿。
3. 在 P1 真实推送链 + 达标数据集上跑既有消融命令：10 配置落盘，指标集完整，提升必须带百分比+样本数+基线绝对值，禁止百分比-only。默认 pytest 不收集该命令。
4. Live 并发：L1 16×120s、≥1200 请求、错误数=0、p95/p99 落盘且不超 B1；全写 16×120s 块总数增量=0。T23 smoke 不算完成。L2 无 LLM 凭证则具名 l2_credentials_missing，禁止静默 skip 当通过。

中文汇报命令与 exit。不要 gh issue。P3 四弹全绿后：对 P1+P2+P3 整轮 diff 做一次 code-review（Standards + Spec，基准 corpus-retrieval 46 SC + corpus-ops-depth SC1–10），再 verification-before-completion。
=== END P3 ===
```
