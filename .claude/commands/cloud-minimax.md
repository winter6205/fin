---
description: 把 Cursor Cloud（及本机）配成走 MiniMax 官方 Chat Completions；只读环境变量，不写 key 进仓库。
argument-hint: "[cloud|local|probe]"
allowed-tools: [Bash, Read, Grep, Glob]
---

# /cloud-minimax

把 **fin** 的 LLM 启动配成 MiniMax **官方 Chat Completions**（`ChatOpenAI` → `POST {FIN_BASE_URL}/chat/completions`）。

程序只读 `os.environ`。本机可用 gitignore 的 `.env` 往环境里填；云端**没有**这份文件，靠 Cursor Secrets + 代码默认值。

`$ARGUMENTS`：`cloud`（默认）只核云端；`local` 核本机 `.env`/export；`probe` 用仓内脚本测 key 是否打得通 Chat Completions。

## 禁止

- 读取、打印、提交任何真实 key / `.env` 内容
- 把 `ANTHROPIC_AUTH_TOKEN=` 写进 `.cursor/environment.json`、start 脚本、测试快照、commit
- 假设 Token Plan（Anthropic Messages / `x-api-key`）能打官网 chat；那条路当前 `fin` **不走**
- 为了「方便云端」把 `.env` 重新 `git add`

## 变量（名字固定）

| 变量                   | 缺省（代码 / `cursor-start.sh` / `.env.example`） | 谁必须设                                     |
| ---------------------- | ------------------------------------------------- | -------------------------------------------- |
| `ANTHROPIC_AUTH_TOKEN` | 无（缺了会变成占位 key，官网失败）                | **必须**：本机 `.env` 或 Cloud Secret        |
| `FIN_BASE_URL`         | `https://api.minimaxi.com/v1`                     | 可不设；海外改为 `https://api.minimax.io/v1` |
| `FIN_MODEL`            | `MiniMax-M3`                                      | 可不设；本地 9router 才用 `fin/MiniMax-M3`   |
| `FIN_CHECKPOINT_URL`   | compose 的 postgres URL                           | 云端 start 会 setdefault                     |

`FIN_BASE_URL` 必须带 `/v1`，不要带 `/chat/completions`。

## 云端怎么做（人点 Dashboard，agent 只核）

1. 确认改动已在 Cloud 跟踪的分支上（通常 `main` 已 push）。
2. 打开 [Cloud Agents Secrets](https://cursor.com/dashboard/cloud-agents)，给**当前 Environment** 增加：
   - 名：`ANTHROPIC_AUTH_TOKEN`
   - 值：用户自己贴（agent 不代填、不回显）
3. **不必**在 Secrets 里再设 `FIN_BASE_URL` / `FIN_MODEL`，除非要覆盖默认官网路径。
4. 新开 Cloud Agent（旧会话不一定吃到刚加的 secret）。
5. 在 Cloud 会话里只测存在性，不测值：

```bash
[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ] && echo ANTHROPIC_AUTH_TOKEN=SET || echo ANTHROPIC_AUTH_TOKEN=UNSET
echo "FIN_BASE_URL=${FIN_BASE_URL:-<unset, code default https://api.minimaxi.com/v1>}"
echo "FIN_MODEL=${FIN_MODEL:-<unset, code default MiniMax-M3>}"
```

6. 启动仍是 `fin-serve` / `python -m fin`：先 `apply_dotenv()`（无文件则 noop），再读 `os.environ`。

## 本机怎么做

1. `.env` 从 `.env.example` 复制（已 gitignore）。只把 key 写进 `.env` 或 `export`。
2. 不要 `git add .env`。
3. 本地 9router（opt-in）：`FIN_BASE_URL=http://localhost:20128/v1` 且 `FIN_MODEL=fin/MiniMax-M3`。

## probe（`$ARGUMENTS` 含 probe）

仓内脚本**不读** `.env`，避免测错 key：

```bash
# 仅当用户已在当前 shell SET 了 token；不要 cat .env
MINIMAX_TEST_KEY="$ANTHROPIC_AUTH_TOKEN" ./scripts/test_minimax_key.sh
```

完成判据：至少 **1/4 或 2/4**（CN/海外 Chat Completions Bearer）为 OK。只有 3/4 或 4/4 OK → 这是 Token Plan Messages key，**不能**当当前默认官网 chat 用。全程只报 HTTP status / VERDICT，不回显 key。

## 本命令执行顺序

1. 读 `src/fin/host/compile.py` 的 `DEFAULT_MODEL` / `DEFAULT_BASE_URL`，以及 `scripts/cursor-start.sh` 的 export，确认与上表一致。不一致先报漂移，再改配置。
2. 按 `$ARGUMENTS` 跑 SET/UNSET 检查（禁止 `echo` token 值）。
3. `cloud`：列出 Dashboard 步骤；Secrets 未 SET 则停下告诉用户去贴，agent 不写 secret。
4. `probe`：仅在 token SET 时跑 `scripts/test_minimax_key.sh`。
5. 中文收尾：改了什么（无则写「只核验」）、SET/UNSET 结果、探针 verdict（若跑了）、未把 key 写入仓库的确认。

## 完成判据

- [ ] 说得出 key 变量名与官网 Chat Completions URL，且与 `DEFAULT_*` 一致
- [ ] 云端路径不依赖仓库里的 `.env`
- [ ] 未打印、未提交 key
- [ ] probe 时 Chat Completions 行有明确 OK/REJECTED（若本轮要求了 probe）
