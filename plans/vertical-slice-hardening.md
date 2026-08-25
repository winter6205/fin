# Plan: vertical-slice-hardening

**Goal:** 已合入的垂直切片（问 filings → 证据 → 带引用回答 → 可靠 workspace）的每个已知遗留缺口被闭合：retrieve seam 的契约缺口全部有绿测试、每个已知错误态渲染为普通人文案（不再有空白 crash 卡或无声 "0 hits"）、存在 opt-in 的 MCP 2.0 协议 smoke——默认 `uv run pytest -q` 始终绿且不碰 live SEC / live memory server。

**Approach:** 本增量是 harden / close-leftover，不是新 epic：先把 retrieve adapter 的信封契约与端口签名收口（T1–T2），在其上落 opt-in MCP 2.0 smoke（T4）；host 侧的断言收紧、prompt 修正、clarify 缺槽注入、错误文案、boot WARNING、dist 去依赖各自是独立可并行的遗留闭合（T3、T5–T10）；web 侧两条（T11–T12）与 host 并行。**host graph 不变**；BM25 保持归档（ADR 0032）；不重开 S1/EDGAR epic；不混入 S2+（cross-period redline / peer matrix / watchlist / charts / multi-agent 等）；`eval` mark 的 marker 配置不动（spec SC18）。Open Questions 为空、ADR 0032 已 accepted，故本 plan 无 `[decision]` 子弹——全部为 `[implementation]`。

**Spec link:** `specs/vertical-slice-hardening.md`
**Implementer model:** `claude-opus-5-thinking-high-fast`（下一 session 执行；本 planner session 不实施）
**Tracker:** FALLBACK — 本地 plan 文件。依 `writing-plans/references/tracker.md` 的 fallback 规则：本环境对 `gh` issue 的写/读返回 403（无可用 issue 写权限），主路径（每 tracer bullet 一个 `ready-for-agent` issue + GraphQL `addBlockedBy` 原生阻塞边）不可达，故 tracker 边只存在于本文件的 `[blocks:]` 标注中，不存在 GitHub issue edges。spec Inherits 的 tracker note 已预告此 fallback。
**Per-ticket loop (all bullets, header 声明一次):** test-driven-development → typecheck+tests（Python 弹：`uv run pytest -q`；标记弹另跑 `uv run pytest -q -m soft_track` / `-m mcp`；web 弹：`npx tsc --noEmit` + `npx vitest run`）→ verification-before-completion → one commit on the ticket branch
**待写入:** empty — 切片过程未引入新 CONTEXT 术语（所有术语从 spec/CONTEXT 原文引用），未发现需 reopen 的 ADR（MCP 2.0 initialize → tools/call 已由 ADR 0032 决定，本 plan 不重开）。

## ACR（自 `specs/vertical-slice-hardening.md` 原样粘贴）

bounded-context-guardian: yes — 全部改动落在既有能力切片 `src/fin/retrieve/`、`src/fin/host/`、`web/src/` 及对应 tests/，无新技术分层目录、无新 bounded context；SC8 反而收紧一处已核实的边界泄漏（`src/fin/retrieve/tool.py:35` 现以具体 `BM25Adapter` 而非 port 作类型），跨切片依赖仍经 **retrieve port** 接口声明。
defensive-contract-validator: yes — Does + Success Criteria 五类边界均有具名测试分配：empty（SC3 空命中信封 → `{chunks: []}`）、negative（SC5 malformed envelope → `retrieve_unavailable`；SC8 签名一致性）、overflow（SC2 收紧 `tests/host/test_soft_track_ship.py:582` 的 `("refuse", "step_cap", "done")` 断言泄漏；SC16 >10 spans 折叠）、concurrent（SC17 409 `concurrent_run` 显式呈现）、exception（SC4 无传输 EXIT、SC10 `numeric_gate_fail` 文案、SC14 boot WARNING）。
error-handling-enforcer: yes — 每条失败路径在 spec 中具名且有类型：`retrieve_unavailable` / `unknown_locator` / 409 `{error: concurrent_run}` / 503 `{error: ingest_missing}` 继承自 `specs/s1-corpus-retrieve.md` 与 `specs/workspace.md` 的 typed EXITs；空命中被显式定义为 miss（`{chunks: []}`）而非错误或 raise；composer 不再吞掉 409/503；不可达 MCP 有 boot WARNING 出口，无空 catch / null-on-failure 意图。
complexity-anti-drift: yes — 声明结构为四焦点各自的小粒度改动（断言收紧、prompt 文本修正、`format_tool_summary` 分支文案、契约测试补齐、opt-in mark + fixture server、web 纯函数抽取以可测），每处停留在单一抽象层；host graph 明确不改，无 god-function / god-file / 横切 handler 意图 —— 按声明结构判定，未使用任何行数估计。
minimal-change-verifier: yes — 单一逻辑增量（收口 009 NOW 四焦点遗留），Does 未混入无关 epic：S2+、重开 S1、host-graph 重写、BM25 复活、live memory 集成均在 Out；FinanceBench 与 Postgres 硬化已由操作者确认 park；tracer-bullet 拆分归 writing-plans。

**OVERALL: PASS — hand to writing-plans**

## Harvest（已决 vs 开放）

**已决（各弹 Inherits 引用原文）：**

- Typed EXITs（`specs/s1-corpus-retrieve.md` / `specs/workspace.md`）：`retrieve_unavailable`、`unknown_locator`、空命中 = `{chunks: []}` / `{filings: []}`、409 `{error: concurrent_run}`、503 `{error: ingest_missing}`。
- ADR 0032（accepted）：retrieve adapter 是 MCP 2.0 client（initialize → tools/call），可对 fixture server 做协议测试；Fake 为 pytest 默认；BM25 归档非 fallback；不与 memory repo lockstep。**本 plan 不设 `[decision]` 弹重开此决定。**
- 产品默认 `DEFAULT_RETRIEVE_ADAPTER = "mcp"`；测试路径 `compile_graph` 默认绑 `FakeRetrievePort`（spec SC7）。
- opt-in mark 命名：spec SC6 留给 plan 定名——**本 plan 定为 `mcp`**（与 spec 示例一致），默认收集必须 deselect 它；`soft_track` / `eval` 维持 opt-in，`eval` marker 配置不改（SC18）。
- FinanceBench / gold 数字与 Postgres 硬化：操作者确认 park，不在任何弹中。

**开放（implementer headroom，不在弹内固化）：**

- fixture server 的形态（子进程脚本 / 内存 stdio 双端）与其文件位置。
- `mcp` mark 的默认排除机制（addopts `-m` 表达式 / conftest collection hook）。
- boot WARNING 的探测点与日志措辞；clarify 缺槽注入的模板文字。
- SPA 测试去 dist 依赖走 skipif 还是 `tmp_path` 挂载；web 侧纯函数抽取的命名与位置。

## Tasks (ordered by dependency)

每个编号项是一颗 tracer bullet：一条可演示的遗留闭合、一个 tag、一个 commit、给 implementer 留 headroom。Acceptance 不含 complexity-anti-drift 阈值数字（那些 gate 归 `complexity-anti-drift` `references/thresholds.md`）。

1. **MCP adapter 信封契约收口（empty / no-transport / 变体全覆盖）** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 (a)(b)(d)："MCP server empty-hit envelope `{result: {hits: []}}` maps to `{chunks: []}` — an empty result is a miss, never an error dict and never a raise"；"the default `_call_mcp` real-transport branch with no configured command (command none/false) returns `{error: \"retrieve_unavailable\"}` — the typed EXIT from `specs/s1-corpus-retrieve.md`, not a crash and not `{chunks: []}`"；"`result.hits`, top-level `hits`, and content-block JSON each map to chunks or a typed error — no silent partial parse"。
   - **Surface:** retrieve 能力切片（`src/fin/retrieve/` 的 MCP adapter seam + 既有 `tests/retrieve/test_mcp_adapter.py`）。
   - **Acceptance:** 三组具名新测试各自可观察（SC3/SC4/SC5）：空命中信封 → `{chunks: []}`（非 error dict、非 raise）；command 未配置/false → `{error: "retrieve_unavailable"}`；`result.hits` / top-level `hits` / content-block JSON 各变体映射为 chunks 或 typed error，malformed envelope → `retrieve_unavailable`。`uv run pytest -q` exits 0，且不请求 `data.sec.gov` / `www.sec.gov`、不需 live memory server（SC1）。
   - **Headroom:** 测试用例的组织方式与 scripted server 的伪造手法由 implementer 选；adapter 内部解析结构可重排，只要上述映射可观察成立。
   - Status: [ ] pending

2. **[parallel] 端口签名对齐：tool.py 依 port 类型、`document_id` keyword-only** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 (f)："`src/fin/retrieve/tool.py` stops typing the archived `BM25Adapter` as the retrieve dependency (types against the port); `MCPRetrieveAdapter.retrieve` matches the port's keyword-only `document_id` parameter shape"；ADR 0032："In-process BM25 (ADR 0007) remains archived, not a fallback"；spec SC9："host tool forwards `{error: \"retrieve_unavailable\"}` and records no evidence for that call"（既有行为保持绿）。
   - **Surface:** retrieve 能力切片（`src/fin/retrieve/tool.py`、MCP adapter 签名 + `tests/retrieve/` 新签名一致性测试）；host 侧既有 `tests/host/test_retrieve_typed_exit.py` 保持绿。
   - **Acceptance:** `src/fin/retrieve/tool.py` 的 retrieve 依赖不再有 `BM25Adapter` 类型标注（依 port 类型，SC8 的 `rg` 检查通过）；新签名一致性测试见证 `MCPRetrieveAdapter.retrieve` 的 `document_id` 为 keyword-only 且与 `RetrievePort.retrieve` 形状一致；`tests/host/test_retrieve_typed_exit.py` 仍通过；`uv run pytest -q` exits 0。
   - **Headroom:** 签名一致性的见证方式（inspect.signature 比对 / Protocol 静态符合）与测试文件命名由 implementer 定。
   - Status: [ ] pending

3. **[parallel] 默认接线正向断言：Fake 默认 + 产品默认 mcp** — tag: `[implementation]`
   - **Inherits:** spec Focus 2 (e)："the test that today only checks `graph is not None` asserts that test-path `compile_graph` binds `FakeRetrievePort` by default (and product default remains `DEFAULT_RETRIEVE_ADAPTER = \"mcp\"`)"；ADR 0032："Default pytest uses a Fake port"。
   - **Surface:** retrieve 测试面（既有 `tests/retrieve/test_compile_graph_adapter_swap.py` 与 `tests/retrieve/test_product_retrieve_wiring.py`；必要时 `tests/host/test_compile.py`）。
   - **Acceptance:** SC7 成立：正向 isinstance/identity 断言见证测试路径 `compile_graph` 默认绑定 `FakeRetrievePort`（不再只断 `graph is not None`）；伴随断言保持产品 `DEFAULT_RETRIEVE_ADAPTER == "mcp"`；`uv run pytest -q` exits 0。
   - **Headroom:** 断言落在哪个既有测试文件、用 isinstance 还是 identity 由 implementer 选。
   - Status: [ ] pending

4. **opt-in MCP 2.0 协议 smoke（mark `mcp` + fixture server）** — tag: `[implementation]`
   - **Inherits:** ADR 0032（已决，不重开）："the product adapter is an MCP 2.0 client (initialize → tools/call), protocol-testable against a compliant public or fixture server — not a joint release with any memory-server repo"；spec Focus 2 (c)："under a new opt-in pytest mark (e.g. `mcp`), never collected by default `uv run pytest -q`"——mark 名本 plan 定为 `mcp`（Harvest 已决项）。
   - **Surface:** retrieve 能力切片（协议 smoke 测试 + fixture server）+ pytest marker 配置（`pyproject.toml` `[tool.pytest.ini_options]`，与既有 `soft_track` / `eval` 同类；`eval` 配置不动，SC18）。
   - **Acceptance:** SC6 成立：`uv run pytest -q -m mcp` exits 0，对 fixture server 走完 MCP 2.0 initialize → tools/call 握手；默认 `uv run pytest -q` 不收集该 mark（由 deselected 计数 / marker 配置可见）且仍 exits 0；公共 server 可选、不需要 live memory server。
   - **Headroom:** fixture server 形态（stdio 子进程 / 内存双端）、文件位置、默认排除机制（addopts 表达式 / conftest hook）全部开放。
   - [blocks: T1, T2] — smoke 行走的 adapter 必须先满足信封契约与端口签名。
   - Status: [ ] pending

5. **[parallel] Soft-Track overflow 断言收紧（去掉 `done` 泄漏）** — tag: `[implementation]`
   - **Inherits:** spec Focus 1："the boundary case that currently also accepts terminal status `done` must assert the actual overflow outcome (`step_cap` / `refuse` on research overflow), not a disjunction that lets `done` pass"。
   - **Surface:** host 测试面（既有 `tests/host/test_soft_track_ship.py` 的 overflow 矩阵边界用例）。
   - **Acceptance:** SC2 成立：`uv run pytest -q -m soft_track` exits 0，且 research overflow 边界断言的接受状态集合不再含 `"done"`（读断言可验证）；如收紧暴露 scripted 剧本缺陷，修剧本而非放宽断言。
   - **Headroom:** 剧本如何驱动到真实 overflow 结局由 implementer 定；不改 host graph。
   - Status: [ ] pending

6. **[parallel] `HOST_SYSTEM_PROMPT` 工具清单修正（+get_xbrl_fact / −BM25）** — tag: `[implementation]`
   - **Inherits:** spec Focus 1："`HOST_SYSTEM_PROMPT` (`src/fin/host/agent_instructions.py`) names `get_xbrl_fact` alongside the other research tools and drops the stale BM25 mention; the prompt text keeps describing the retrieve seam as the **retrieve port** (per ADR 0032). This is prompt-text correction, not a glossary change."
   - **Surface:** host 能力切片（`src/fin/host/agent_instructions.py` + 既有 `tests/host/test_agent_instructions.py`）。
   - **Acceptance:** SC12 成立：prompt 文本含 `get_xbrl_fact` 且不含字符串 `BM25`，由 `tests/host/test_agent_instructions.py` 新断言见证；retrieve seam 描述仍是 retrieve port；`uv run pytest -q` exits 0。
   - **Headroom:** prompt 句式与工具描述措辞由 implementer 写；断言只锁两条可观察性质。
   - Status: [ ] pending

7. **[parallel] clarify 轨缺槽注入 call-scoped prompt** — tag: `[implementation]`
   - **Inherits:** spec Focus 1："Clarify-track prompt injects the current missing slots (from the latest status snapshot / manifest gaps) into the call-scoped prompt, instead of a static ask."；CONTEXT **status snapshot**："the latest snapshot is the active view for routing; dependent fields may be empty on a new snapshot"。
   - **Surface:** host 能力切片（clarify 提示构造所属模块 + `tests/host/` 新测试）。
   - **Acceptance:** SC13 成立：新 host 测试用脚本化 snapshot 制造已知缺槽 → 该轮 call-scoped prompt 文本点名该缺槽（不再是静态问句）；`uv run pytest -q` 与 `uv run pytest -q -m soft_track` 均 exits 0。
   - **Headroom:** 缺槽如何从 snapshot/manifest 推导、注入模板措辞、helper 抽取与否全部开放；不改 host graph 拓扑。
   - Status: [ ] pending

8. **[parallel] 错误终局与工具摘要的普通人文案（numeric_gate_fail + format_tool_summary）** — tag: `[implementation]`
   - **Inherits:** spec Focus 3："`numeric_gate_fail` gets ordinary-person copy — a numeric-gate refusal must never render a blank crash card (blank terminal copy is already forbidden by `specs/workspace.md` Success Criteria)"；"`format_tool_summary` renders `retrieve_unavailable` distinguishably from 0 hits (e.g. \"retrieve backend unavailable\" vs \"0 hits\"), and gives `get_xbrl_fact` a human-readable one-liner instead of falling through to the bare tool name"。两处同属 host 的 stream 呈现映射，须一起改故同弹。
   - **Surface:** host 能力切片（stream 映射 / `format_tool_summary` 所属模块 + 既有 `tests/host/test_stream_map.py` 与 `tests/host/` 新终局文案测试）。
   - **Acceptance:** SC10 + SC11 成立：以 `numeric_gate_fail` 结束的 run 在 turn row / SSE 终局帧产生非空普通人文案（无空白助手卡）；`retrieve_unavailable` 的摘要文本 ≠ 0-hit 摘要文本；`get_xbrl_fact` 得到人类可读一行摘要（非裸工具名）——均由新 host 测试见证；`uv run pytest -q` exits 0。
   - **Headroom:** 具体文案措辞、映射表还是分支实现由 implementer 定；citation verifier 本体一字不动。
   - Status: [ ] pending

9. **fin-serve boot WARNING + `FIN_RETRIEVE_ADAPTER` 文档** — tag: `[implementation]`
   - **Inherits:** spec Focus 3："`fin-serve` boot with `FIN_RETRIEVE_ADAPTER` defaulting to `mcp` but no reachable MCP command logs a boot WARNING (same class as the existing missing-`web/dist` warning); README and `.env.example` document `FIN_RETRIEVE_ADAPTER`."
   - **Surface:** host 能力切片（serve 启动侧：`workspace_http.py` / `operator_env.py` 一侧 + `tests/host/test_workspace_http.py` 或 `test_operator_env.py`）+ 仓库文档面（README、`.env.example`）。
   - **Acceptance:** SC14 成立：adapter 为 `mcp` 且无可达 MCP command 的启动路径发出一条 WARNING 日志记录（新测试捕获）；`rg -l "FIN_RETRIEVE_ADAPTER" README.md .env.example` 两个文件均命中；`uv run pytest -q` exits 0。
   - **Headroom:** 探测"无可达 command"的位置与 WARNING 措辞开放；不把 WARNING 升级为启动失败。
   - [blocks: T1] — "无传输"条件的类型化语义（`retrieve_unavailable` EXIT）先由 T1 固化，boot WARNING 与之口径一致。
   - Status: [ ] pending

10. **[parallel] SPA catch-all 测试去 `web/dist` 硬依赖** — tag: `[implementation]`
    - **Inherits:** spec Focus 3："SPA catch-all tests no longer hard-depend on a committed `web/dist`: default pytest stays green without `dist` (skipif or `tmp_path`-mounted dist)."
    - **Surface:** host 测试面（workspace HTTP 的 SPA catch-all 用例）。
    - **Acceptance:** SC15 成立：把 `web/dist` 临时移开后 `uv run pytest -q` 仍 exits 0（移回后同样绿）。
    - **Headroom:** skipif 还是 `tmp_path` 挂载 dist 由 implementer 选（spec 明示两者皆可）。
    - Status: [ ] pending

11. **[parallel] Composer 409/503 发送失败可见呈现** — tag: `[implementation]`
    - **Inherits:** spec Focus 3："Composer surfaces 409 / 503 send failures to the operator instead of swallowing them after clearing the draft."；继承 typed EXITs："workspace 409 `{error: concurrent_run}` / 503 `{error: ingest_missing}`"（`specs/workspace.md`）。
    - **Surface:** web 能力切片（`web/src/` composer 相关组件/逻辑 + 新 vitest）。
    - **Acceptance:** SC17 成立：新 vitest 见证 composer 发送收到 409 或 503 时渲染可见错误态（清空草稿后无声吞掉的路径消失）；`npx vitest run` 与 `npx tsc --noEmit` 在 `web/` exits 0。
    - **Headroom:** 错误态的 UI 形式（inline 提示 / toast）、是否保留草稿由 implementer 定，只要失败对操作者可见。
    - Status: [ ] pending

12. **[parallel] Sources 折叠 + last-thread restore 的 web 测试** — tag: `[implementation]`
    - **Inherits:** spec Focus 3："New web tests (vitest, alongside `web/src/turnStream.test.ts`): sources-list collapse behavior for >10 citation spans; last-thread restore on returning visit. Exporting a pure function to make these testable is implementer headroom."
    - **Surface:** web 能力切片（`web/src/` + 新 vitest）。
    - **Acceptance:** SC16 成立：新 vitest 见证（i）一个 turn 的 citation spans >10 时 sources list 折叠；（ii）回访恢复仍存在的上次 `/t/{thread_id}`；`npx vitest run` 与 `npx tsc --noEmit` 在 `web/` exits 0。
    - **Headroom:** 纯函数抽取与命名（spec 原文即声明为 implementer headroom）；折叠交互细节不在断言内。
    - Status: [ ] pending

## End-of-round: code review phase（一次，非每弹）

全部 12 弹落地后，对整轮 diff 做一次 **code-review**（Standards + Spec 双轴，依 `.cursor/skills/code-review/SKILL.md` 并行两个 reviewer subagent）；review 基准为 `specs/vertical-slice-hardening.md` 的 18 条 Success Criteria 与仓库 Standards。每弹自身的 loop 只到 verification-before-completion + one commit，不重复 per-bullet review。
