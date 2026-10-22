# 代码质量（fin）

通用原则（复杂度、错误处理、不顺手重构、不提前抽象、查重复）以用户体系为准，这里只写本仓事实和红线。

## 红线

1. **依赖方向**（import 方向和自检命令见 `AGENTS.md` 硬边界第 14 条）：合法方向以 `docs/context-map.md` 为准；adapter 类只在 `src/fin/host/compile.py`、`graph.py` 里选择和构造，其他地方只用 retrieve port 类型。
2. **不依赖 iknow-memory 代码**：只经 retrieve port 和知识 MCP 访问，不新增对它的 import。
3. **存储格式改动先问**：PostgreSQL 表结构和各 store（thread、memory、attachment、attachment job、metadata）的落盘格式可以改，不用兼容旧数据，也不写迁移；但动手前必须先停下来问用户，用户批准了才能改；改完同步更新 seed 逻辑（见 `test-storage.md`），并在完成汇报里写明改了哪些表或字段。
4. **注释简洁优先，不成段堆叠注释，有旧的无用注释删除。

## 本仓事实

- 模块地图（context map）是 `docs/context-map.md`，术语以 `docs/CONTEXT.md` 为准；新代码放进对应现有上下文，新建上下文或改依赖方向先给计划，确认后同步更新这两份文档。
- host 按业务能力成包：`src/fin/host/{application,attachment,evidence,grounded,question,session,task}/`；能力包不 import `fin.host.http`、`fin.host.application` 与图编译入口，能力之间的依赖边必须与 `scripts/check_capability_boundaries.py` 登记的一致。
- 上一条里的"能力包"指 attachment、evidence、grounded、question、runtime、session、task 七个能力包。`src/fin/host/application/` 是应用边界，拥有应用组装与生命周期，按计划 T3 它要 import 图组装入口（`src/fin/host/application/composition.py` 从 `fin.host.graph` import `compile_graph`），因此豁免"不 import 图编译入口"这一条；其余七个能力包不豁免，也不得 import `fin.host.application`。
- 环境变量读取分散在多个文件，没有统一配置模块；新代码读环境变量要有默认值或缺失时明确报错，不新建配置模块（除非用户要求）。
- 运行期开发数据落在 `~/.fin/` 和 `data/corpus/`；路径一律可注入，新代码不直接回退到 `$HOME`。

## 检查

- Python：新增和改动的代码通过 `npm run typecheck`（pyright）。Python 侧没有自动复杂度检查。
- `web/`：新增和改动的代码通过 `web` 的 tsc 和 `npm run lint:s5`（只比较本次碰到的函数改动前后，不变差即通过；只查 TS/JS）；`lint:s5:all` 的存量问题只列进汇报，不在本任务修。
- `lint:s5` 失败时只在本次新增的分支内调整；要拆分或改任务范围外的代码才能过，先停下来问用户，不得用 `HUSKY=0` 或改检查配置绕过。退出码 2 是配置问题，停下来报告。
- 用户批准拆分后，被搬动的代码按 `test.md` 先有特征测试（没有就报测试缺口，不拆）；修复和拆分分两个提交。
- 完成汇报写 S5 命令、改动前后违规数，单列为过 S5 做的改动。
