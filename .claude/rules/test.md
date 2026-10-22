# 测试规则（常驻）

按改动风险分层验证，以真实、可追溯的证据判定完成。交付前跑到适用改动要求的最高层；删测试、放宽断言、加 skip 等改动的限制见 `AGENTS.md` 硬边界。CI、hook 和单次通过只证明其实际执行范围。测试选择、结果和未覆盖项必须如实报告。

| 层 | 命令 | 用途与适用要求 |
|---|---|---|
| L0 | `npm run typecheck`（pyright，只查 `src/fin`）；改 `web/` 时加跑 `npm --prefix web run typecheck` | 最快；所有代码改动 |
| L1 | `uv run pytest <文件>::<用例>`；`web/` 下 `npm --prefix web test -- <文件>` | 快速回归；每轮实现和调试 |
| L2 | `npm run typecheck:staged` 和 `npm run lint:s5`（S5 只查 `web/` 的 TS/JS；pre-commit 跑 staged typecheck 与 S5） | 中等；迭代交付前。使用 `HUSKY=0` 时手动跑并报告 |
| L3 | `uv run pytest -q`；改 `web/` 时加跑 `npm --prefix web test` | 慢；所有代码改动。opt-in 标记默认不跑；只改文档、规格、计划或根目录 Markdown 可以不跑 |
| L4 | `uv run pytest -q -m <标记>`：`eval`、`memory_on`、`soft_track`、`mcp`、`sql_live`、`jev_live` | 最慢；改模型可见内容必跑 `eval`、`memory_on`；`sql_live`、`jev_live` 在改到对应模块时按场景细则跑 |

## 预检、竞争与持久化

- 长耗时或多阶段 campaign 开始前做 fail-fast 预检：依赖与导入、fixture 来源及重建方式、数据库测试身份、待测 request/event seam 契约、服务就绪状态；依赖浏览器时加一次短浏览器/API smoke。任一前置失败就停止长跑，报告 `blocked` 或 `Not run`，不得把未运行的覆盖记为通过。
- 多阶段长测为每个阶段声明时限和可观察状态；时限须来自代表性测量，未测量前不宣称延迟 SLO；没有 runner 或证据采集能力时该门禁记 `Not run`。
- 并发、乱序、重连、取消和重复提交用 barrier 或显式控制点安排顺序并记录先后，不靠 `sleep` 碰竞态；用例要覆盖切会话、迟到回调和取消后的写入这类完成顺序被改变的场景。断言业务契约和最终归属，供应商私有实现细节只在适配器测试中断言。
- 验证持久化必须由新启动的 OS 进程读回，同一进程内重建 store 不算跨进程恢复；并覆盖中途故障点（进程退出、重启后从持久状态恢复）。同一对象或进程内仍能读到不证明已经持久化。存储、Runtime、checkpoint 与解析任务的具体做法见 `.claude/rules/test-storage.md`。
- 声称 Agent UI 的会话连续性或异步状态可靠时，必须有真实浏览器 journey 证据并同时核对界面结果与持久状态；场景清单见 `.claude/rules/test-web.md`。

## 证据与汇报

- 每个验收 campaign 执行前固定 case ID/版本、输入与 fixture 版本、run manifest 和 rubric；每个 case 结束立即持久化其 case/version、manifest、attempt、状态、rubric 版本、证据引用和脱敏失败分类，历史 run 追加保存不覆盖。验收集规模、指标口径和 bad case 流程见 `.claude/rules/test-model-visible.md`。
- 中断后保留已完成 case 的证据，恢复只补同一 manifest 中未完成的 case；重跑记为新 attempt，不覆盖原失败、不靠重试跑绿；只有重跑才通过的记 `flaky`，不算干净通过。
- 机制测试、Fin 业务验收和浏览器 journey 分开报告结果与分母，指标说明数据来源、分子和分母；诊断 trace 不代替规范化的 attempt/outcome 记录。
- 汇报每条实际命令、层级、退出码及通过、失败、跳过、`flaky`、`not_run`、`blocked`、中断数量，并列出跳过项与原因。CI 只证明 workflow 实际运行的 lane；当前 CI 可用性与覆盖范围见 `AGENTS.md`，不可用时按未运行报 `Not run`。缺 runner、依赖、fixture、服务或密钥时写 `Not run: <层/场景> <原因>`（`sql_live`、`jev_live` 缺实例或缺 key 按具名失败处理，不记 `Not run`），不得把计划中的覆盖描述为已通过。测不了的能力登记到 `docs/test-gaps.md`，说明缺什么能力和影响哪一层；不写 key 名、本机路径、连接串。

## 红线与场景路由

- 测试不连日常数据库、不写真实 `~/.fin/`：只连库名以 `_test` 结尾的库，测试库与临时根目录要求见 `AGENTS.md` 硬边界和 `.claude/rules/test-storage.md`。真模型和真服务测试默认授权，该跑就跑，不为费用停下来问；`sql_live`、`jev_live` 缺实例或缺 key 是具名失败，不得改成 skip。凭据与测试数据边界见 `.claude/rules/security-boundaries.md`。
- 要改没有测试覆盖的旧代码时，先写特征测试钉住现有可观察行为；可疑现状写进汇报，不顺手改。
- 改 store、`~/.fin`、PostgreSQL、ingest 时读 `.claude/rules/test-storage.md`；改 prompt、工具描述、schema、黄金集时读 `.claude/rules/test-model-visible.md`；改 `web/` 时读 `.claude/rules/test-web.md`。
