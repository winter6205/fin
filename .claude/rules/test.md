# 测试规范

新增或修改业务逻辑时，必须优先补充测试。测试应覆盖：

1. 正常路径
2. 失败路径
3. 边界条件
4. 权限不足
5. 空输入或非法输入
6. 并发或重复提交场景（若适用）

不得为让构建通过而删除测试或降低 assert 强度；删除/替换测试必须覆盖不变或更强，且附理由 + 授权 + commit 正文说明。不得把失败测试改成 skip（除非明确授权 + 说明原因）。

如果测试无法运行，必须记录：

```text
Validation:
- Not run: <reason>
- Expected command: <command>
- Blocking issue: <issue>
