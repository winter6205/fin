---
paths:
  - "docs/guides/prompt-development.md"
  - "docs/guides/grounded-assessment-prompt-requirements.md"
  - "src/fin/host/agent_instructions.py"
  - "src/fin/host/compile.py"
  - "src/fin/host/memory_*.py"
  - "src/fin/host/tools/**"
  - "src/fin/eval/**"
  - "tests/eval/**"
  - "tests/host/*tool*"
  - "tests/host/*gold*"
  - "tests/host/*memory*"
  - "tests/host/*grounded*"
  - "tests/host/*assessment*"
  - "tests/host/*prompt*"
  - "tests/host/*agent_instructions*"
  - "data/grounded_assessment/**"
---
# 模型、工具与业务验收证据

用规范输入边界与版本化业务验收证据评价模型和工具质量；调用量、采样 trace 与单轮回执不能充当质量分母。

- 改 prompt、说明书、工具描述或 schema、记忆预取时，先读 `docs/guides/prompt-development.md` 并跑对应黄金集；没有对应集合时补覆盖，或在 `docs/test-gaps.md` 登记。
- 黄金集命令：`uv run pytest -q -m eval tests/eval/`（`test_gold.py`）；`uv run pytest -q -m memory_on tests/host/test_memory_gold_set.py`。模型可见内容改动仍按 `test.md` 必跑 `eval` 与 `memory_on`；`jev_live`、`sql_live` 按 `test.md` 的场景范围执行。
- 假模型（如 `scripted_model.py`、`jev_fake.py`）只提供协议和工具层证据；涉及模型选择、纠错或最终回答的判断还须有适用的真模型证据。密钥按 `security-boundaries.md` 处理。

## 工具参数与执行边界

- 每个工具只维护一个参数 schema 来源：简单参数从函数注解生成；嵌套或非平凡参数显式声明 Pydantic `args_schema`。模型暴露 schema 与运行时校验必须一致，不另抄一份 schema 或并行手写校验器。
- 用实际组装的 `ToolNode` 或图调用入口覆盖无效参数；只测自定义校验函数不足以证明模型调用链行为。验证应在副作用前完成。测试需覆盖安全、带类型的纠错反馈、每个逻辑工具调用默认最多一次模型纠错，以及纠错耗尽后的终态；断言反馈和记录不含原始 kwargs、提交值或异常 repr。
- 将模型参数纠错、工具执行重试和未知副作用结果的对账分别验证。对账未确认前不得把未知副作用当作可安全重试；重试需沿用同一个业务操作身份。
- 工具质量的完整分子、分母来自持久化的 canonical attempt/outcome 记录，并区分逻辑工具调用、模型发出的每次尝试和每次执行尝试。当前五个 OpenTelemetry spans 属于可采样、可缺失的诊断信号；turn receipt 是单轮汇总，也不能代替逐次工具结果账本。
- 至少分别报告以下指标，并固定口径后再比较：

  | 指标 | 分子 | 分母 |
  |---|---|---|
  | 首次参数有效率 | 首次模型工具尝试通过规范 schema 的数量 | 到达该校验边界的首次模型工具尝试数 |
  | 执行成功率 | 成功的执行尝试数 | 全部执行尝试数；另按逻辑操作报告最终结果 |
  | 纠错成功率 | 一次纠错后满足参数及结果要求的逻辑调用数 | 首次参数无效且允许纠错的逻辑调用数 |
  | 必需工具覆盖率 | 选择预期工具且参数有效的要求数 | rubric 标记为必需该工具的机会数 |
  | 业务任务成功率 | 满足冻结 rubric 的必测 case 数 | 声明清单中的全部必测 case 数 |

## Campaign 指标口径

- campaign 的 case 清单、manifest 固定、逐 case 证据保存、中断恢复与 flaky 判定口径按 `test.md`；本文件只补充指标口径。
- 缺失、阻塞、未计分的必测 case 保留在业务任务分母中，不能算通过；执行重试按尝试和逻辑操作分别报告，避免重试抬高成功率。

## Fin 业务验收集与 bad case

- 持续维护版本化的 Fin 业务验收集，测量前冻结 rubric；case 数量不是通过门槛，规模按最终验收范围确定，条数不足时如实报告分母，不为凑数改写同一问题；具体的条数要求属验收范围定义，见 [ADR 0103](../../docs/adr/0103-durable-runtime-and-parser-ownership.md)。最终活动只有在所有必测 case 按声明运行通过时才通过。
- 一条 case 对应一项独立业务判断；改写同一问题、机制测试和浏览器旅程另行计数，不计入验收集条数。合成验收 case 不计入真实且获准的 **评测数据集**；保留该术语对真实语料、来源记录与可重建性的既有定义。
- bad case 持续进入：记录隐去敏感内容的来源与失败标识，缩减为可复现输入，确认预期行为后新增版本化回归 case；保留原失败及后续修复运行，不能为了匹配候选输出改写黄金期望。

依据：操作者于 2026-10-20 确认的工具校验与验收决策；见 [ADR 0103](../../docs/adr/0103-durable-runtime-and-parser-ownership.md) 和 [Context 中的 eval 术语](../../docs/CONTEXT.md#corpus-and-eval)。本规则描述目标测试证据，不表示工具账本、纠错机制或验收集已实现或已通过。分层、汇报格式与红线见 `test.md`。
