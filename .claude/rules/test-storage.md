---
paths:
  - "src/fin/host/**/*_store.py"
  - "src/fin/host/compile.py"
  - "src/fin/host/task/task_runtime.py"
  - "src/fin/host/db_pool.py"
  - "src/fin/host/workspace_registry.py"
  - "src/fin/host/user_profile.py"
  - "src/fin/host/operator_env.py"
  - "src/fin/corpus/**"
  - "tests/conftest.py"
  - "tests/corpus/**"
  - "tests/host/*store*"
  - "tests/host/*postgres*"
  - "tests/host/*checkpoint*"
  - "tests/host/*runtime*"
  - "tests/host/*durable*"
  - "tests/**/*_live*"
---
# store、Postgres 与 `~/.fin` 的测试证据

通用红线在 `test.md`；本文件补充存储、持久化与 Runtime 的具体做法和边界。

- 默认 `pytest -q` 走进程内 store（`tests/conftest.py` 设 `FIN_CORPUS_INGEST_STORE=memory`）；Postgres 用例只连 `_test` 库。运行前先检查 `FIN_TEST_DSN`；未设置时，先查主 checkout 和已授权的测试服务，不能据此断言没有测试库。对既有测试容器只读枚举候选库，再用只读连接核验 `SELECT current_database()` 的结果以 `_test` 结尾。若从现有 operator settings 取得连接参数，只在内存中把 database name 替换为已发现的 `_test` 库名，核验后再只通过进程环境 `FIN_TEST_DSN` 传给测试；不得把原开发 checkpoint 连接直接交给测试。连接值不得写入文件、参数或日志；不读取 `.env`，不打印或记录完整 DSN。可参考 `scripts/demo_acceptance_db_check.py` 的 env-only `current_database()` 身份预检（该 checker 的连接变量仅适用于 demo acceptance）。容器维护库可用以下只读查询列出候选名称：
  ```bash
  docker exec -u postgres <container> psql -XAt -U <role> -p <port> -d postgres \
    -c "SELECT datname FROM pg_database WHERE right(datname, 5) = '_test' ORDER BY datname"
  ```
  要测持久化路径，使用带 `postgres` / `durable` 的用例；声称数据可跨进程恢复时，必须由新启动的 OS 进程读回，单在同一进程里新建 store 不足以证明重启恢复。对相关提交点注入进程退出，再重启并从持久状态恢复；不要保留原进程内存对象来模拟重启。
  若已有 `_test` 库包含 `fin_instance` 身份，应先只读核验其 `instance_id`，再将同一值写入临时 `FIN_HOME`；不得关闭身份校验或触碰真实 `~/.fin/`。
- `sql_live` 同样只连 `_test` 库；改 ledger、structured evidence 落库时跑 `uv run pytest -q -m sql_live <相关文件>`。durable receipt 测试若会删除整张表，先核实该库专供本次测试且没有其他测试连接；否则不要执行整表删除。
- 验证 Runtime、checkpointer 或解析任务的持久所有权时，用真实 PostgreSQL `_test` 库、真实 Redis 和至少两个 OS 进程；用 barrier 控制领取、续租、接管、取消、删除及迟到写入的先后。每个图 checkpoint 写入路径都要验证 generation-aware adapter 在同一数据库事务中拒绝失去所有权的 `put`、`put_writes` 和其他变更；adapter 未接入、不可用或可能绕过时必须失败关闭。不得用 mock、进程内锁或测试 spy 推断框架已经提供 fencing。
- 覆盖 PostgreSQL outbox/inbox 与 Redis Streams 的提交、发布、消费、确认之间的崩溃窗口；重复和乱序事件必须幂等，丢失或裁剪的通知必须能从 PostgreSQL 对账恢复。验证事件游标无法证明连续时返回 snapshot/reset 信号；队列确认或 Redis 中仍有消息本身不证明业务结果已持久化。
- 验证解析结果时，完成通知只表示待消费。先确认解析结果引用可从解析服务持久读取，再验证调用方的结果采用和最终 `ready` 提交分别以短事务锁定并 CAS 检查当前 generation、内容版本/hash、job 关联及 tombstone/cancel 状态；索引等必要步骤完成前不能进入 `ready`。在拉取/校验后、结果采用后分别设置 barrier，让删除或内容替换先提交，再断言旧结果既不能关联到新内容，也不能发布 ready。用进程重启和重复通知验证 inbox 对账及迟到结果处理。
- 工作区、用户画像的根目录一律注入 `tmp_path`，不落到默认的 `~/.fin/`（`workspace_registry.py` 的注释要求测试注入）；断言写入路径在 `tmp_path` 下。
- 语料测试路径用 `FIN_CORPUS_*_PATH` 指到临时目录，不把临时生成内容写进 `data/corpus/`。优先复用仓库现有的 `fixtures/demo-corpus/`：其中有 20 篇 ZEUS 合成文档和 `expected.json` 验收台账。seed 实现在 `src/fin/corpus/demo_seed.py`，通过现有 `npm run seed:demo` 入口（`scripts/seed_demo.py`）调用；向隔离开发栈写演示数据时复用该入口，不另造命令。
- fixture 可使用合成内容，或历史测试明确需要且来源已授权的维护者语料；禁止复制真实用户会话、上传附件或开发/生产库数据。`README.md` 的 “Corpus and validation” 说明维护者 bundle：根目录含 `values/<sha256>.json`（供 `FIN_PRIVATE_FIXTURES` 使用）及历史 `data/corpus/` manifest 和抽取产物。运行依赖该 bundle 的历史测试前，优先从主 checkout 将已授权语料复制到隔离测试树，无需重新下载，也不要提交数据；若已有 `data/corpus/`，不得覆盖或替换它。
- 存储格式可以改、不兼容旧数据（先问用户，见 `code-quality.md`）；改了要同步更新 seed 命令的生成逻辑和对应测试。
- 会回退到 `$HOME` 的代码见 `docs/test-gaps.md`；改到时先补注入口。
- Runtime/checkpoint 与解析 readiness 的持久化要求依据 [ADR 0103](../../docs/adr/0103-durable-runtime-and-parser-ownership.md)；上述测试是目标架构的验收证据，不代表当前代码已实现多进程 fencing 或解析服务。测试分层和最终汇报见 `test.md`。
