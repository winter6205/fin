---
paths:
  - "web/**"
---
# `web/` 前端测试

- `web/vitest.config.ts` 使用 `node` 环境，只收 `src/**/*.test.ts`。`npm --prefix web test -- src/<文件>.test.ts` 是单测信号，不证明浏览器交互或真实页面状态正确。
- 仓库根目录的 `npm run test:stream`（`node --test tests/run-stream-checks.test.mjs`）用假 Playwright CLI 测 SSE 代理与浏览器 runner 的契约，不打开浏览器；它通过不等于浏览器行为正确。
- `node scripts/run-scripted-e2e.mjs` 才会启动本地 SPA/API 并用 Playwright CLI 驱动脚本模型，场景为 `grounded`（默认）、`bounce`、`document_choice`；检查项是 1-home-chrome、2-stream-no-retract、3-reload-persist、4-dom-separation、5-trace-collapse、6-boot-status、7-stream-scroll-follow、8-scroll-follow-lifecycle，加上 bounce 与 document-choice 各自的一项。它是窄浏览器通道，不覆盖完整会话生命周期。
- `bash scripts/demo-browser-acceptance.sh`（`scripts/demo-browser-acceptance.mjs`）是 ZEUS demo 的真实浏览器验收 runner：先校验 PostgreSQL 身份、不 mock API，覆盖可见 demo chrome、带引用与拒答的答案、同线程追问、澄清、开发者回执和 demo 重置。它只覆盖 demo 范围，不能替代下面要求的完整 journey。
- 修改或验收用户可见的会话生命周期时，必须用真实浏览器跑完整 Agent UI、应用 API、持久化测试服务和可控模型/工具的 journey：连续完成至少 3 轮；回到首页并重新打开同一会话，再完成至少 3 轮且保留既有内容；让两个会话同时运行并按相反顺序完成；在运行中刷新/重连；让新建会话后的旧 GET 延迟返回；分别验证 5xx/超时与真正 404；覆盖双击、Enter 加点击及丢失响应后的重试，确认一个稳定操作 ID 只产生一个持久业务操作（HTTP 请求可以多于一次）；验证显式取消与迟到完成的竞争；覆盖附件上传失败、accepted/preparing、ready 和解析失败，刷新后状态仍正确。两个标签页订阅同一运行时应各自看到完整事件。
- 竞争时序按 `test.md` 的 barrier 规则控制响应和事件顺序，不以固定 sleep 代替。断言页面呈现和持久化 API 状态；源码字符串检查、Node 单测或 SSE 回放代理均不能代替浏览器 journey。手动观察只能补充证据；必需浏览器通道未运行时记为 `Not run`，不得据此判定验收通过。
- S5 只检查这里的 TS/JS；`npm run lint:s5` 失败的处理见 `code-quality.md`。测试分层和汇报格式见 `test.md`；本规则依据 [ADR 0103](../../docs/adr/0103-durable-runtime-and-parser-ownership.md)、[QA 测试缺口](../../docs/test-gaps.md) 及仓库中的 [Vitest 配置](../../web/vitest.config.ts) 和 scripted Playwright runner。
