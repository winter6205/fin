# fin 规则结构

| 文件 | 加载 | 负责人 | 行数 | 估算 token |
|---|---|---|---|---|
| `AGENTS.md` | 常驻（经 `@AGENTS.md`） | 工作流（第 14 条：架构） | 55 | ≈1.74k |
| `CLAUDE.md` | 常驻 | 工作流 | 5 | ≈0.04k |
| `.claude/rules/test.md` | 常驻 | 测试 | 21 | ≈0.82k |
| `.claude/rules/code-quality.md` | 常驻 | 架构 | 24 | ≈0.74k |
| `.claude/rules/security-boundaries.md` | 常驻 | 安全 | 45 | ≈1.17k |
| `.claude/rules/test-storage.md` | paths | 测试 | 25 | — |
| `.claude/rules/test-model-visible.md` | paths | 测试 | 21 | — |
| `.claude/rules/test-web.md` | paths（`web/**`） | 测试 | 9 | — |
| `scripts/lint-s5.sh` | — | 工作流（复用 iknow） | — | — |
| `docs/test-gaps.md` | 按需 | 测试 | — | — |

常驻合计约 4.5k token（粗估）。

## lint:s5（照 iknow）

package.json 现状（副本）写死 marketplaces 路径，三条改为：

```
"lint:s5": "sh scripts/lint-s5.sh --changed",
"lint:s5:staged": "sh scripts/lint-s5.sh --changed --staged",
"lint:s5:all": "sh scripts/lint-s5.sh web/src"
```

`scripts/lint-s5.sh` 直接复用 iknow 那份。检查器 `eslint.config.mjs` 只匹配 `**/*.{ts,tsx,js,mjs,cjs}`，所以只对 `web/` 生效。
pre-commit 现状（副本）：`npm run lint:s5:staged && npm run typecheck:staged`。

## 不照搬 iknow 的

- 「配置统一经 `src/config/`」：fin 无此模块。
- 「`src/shared`、`src/util`」：fin 无此类目录。
- TUI 相关红线：fin 无 TUI。

## AGENTS.md 硬边界「依赖方向」条（建议原文）

`src/fin/corpus` 不得 import `fin.host`、`fin.eval`；`src/fin/host`、`src/fin/retrieve` 不得 import `fin.eval`；自检 `rg -n "^\s*(from|import) fin\.(host|eval)\b" src/fin/corpus` 和 `rg -n "^\s*(from|import) fin\.eval\b" src/fin/host src/fin/retrieve` 应无输出。

副本实测：上述四个方向均为 0。`retrieve → host` 精确 import 有 4 处（`mcp_push_port.py:61`、`mcp_index_job_port.py:32`、`mcp_invalidation_port.py:52` 借用 `fin.host.compile.DEFAULT_MCP_COMMAND`；`mcp_adapter.py:670` 函数内 import `fin.host.evidence_contract`），context-map 未列此方向，作为已知偏差登记，不进红线，是否修由用户定。

