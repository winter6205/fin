# 安全边界（fin 特有细则）

红线在 `AGENTS.md`「硬边界」，这里只写细则。

## 读取与环境变量

- 确认环境变量是否存在用 `[ -n "${VAR:-}" ] && echo set || echo unset`，不用 `echo "$VAR"`、`env`、`printenv`、`set -x`。
- 不用 `python3 -c`、`uv run python -c`、`grep -r`、`source .env` 等写法绕开 `.env` 读取限制；settings 的 deny 拦不住这些写法，靠本条约束。
- 测试只读 `FIN_TEST_DSN`，库名必须以 `_test` 结尾（见 `AGENTS.md` 第 4 条）；确认时只看库名，不打印完整连接串，也不为此读取 `.env`。

## 测试数据与凭据

- 测试、fixture、快照、示例里的凭据用明显的占位值，或运行时从环境变量读取。
- 真模型或真服务标记的 key 从环境或 settings 配置读，不打印、不写进文件；这些标记的默认授权与执行纪律见 `test.md`。

## 公开镜像

- 开发在私有仓（`origin`）；公开镜像 `winter6205/fin` 只在用户对该次同步明确授权后，从 `${RELEASES_DIR:?}` 下的发布工作区推送，不在开发仓加镜像 remote。`.claude/`、`CLAUDE.md`、`AGENTS.md` 会进镜像（发布流程规则 `.claude/rules/open-source-publish.md` 是内部流程，不进镜像）：规则、代码、文档里不写内部名称和本机路径；内部名称清单只在 `${RELEASES_DIR:?}/internal-patterns.txt`（与 iknow 共用；文件尚未建立，见 CHANGES 落地项），不进仓库，命中时只报文件和行号。
- 自检（仓库根目录，只读，输出为空才通过）。占位形式 `/home/operator/` 与 `/mnt/c/` 是公开仓的既定匿名写法，不是本机路径，不计为命中：

```bash
test -r "${RELEASES_DIR:?}/internal-patterns.txt" && git grep -n -I -i -E -f "${RELEASES_DIR:?}/internal-patterns.txt" -- . ':!*.lock'
rg -n -uu -P --glob '!.git/' --glob '!**/node_modules/**' --glob '!.venv/' 'projects/release[s]|/home/(?!operator/)[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/|/mnt/(?!c/)[a-z]/|\b[A-Z]:\\' .
```

- 发布前在发布工作区再跑：上面第一条、`git rev-list --all | xargs -r -n 100 git grep -l -I -E -f "${RELEASES_DIR:?}/internal-patterns.txt"`（历史）、`ls -a | rg '^\.env' | rg -v '\.env\.(example|sample)$'`，任一有输出即停止发布，把结果附在批准请求里；发布流程、引用卫生、强制推送与验收口径见 `.claude/rules/open-source-publish.md`。
