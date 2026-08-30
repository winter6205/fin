# 0039. Retrieval capabilities default to all-on; capability switches exist only as an eval dimension

Date: 2026-08-31
Status: accepted

fin 的产品调用路径不携带任何关闭检索能力的参数，能力一律默认全启用；能力开关只作为**消融**的评测维度出现在评测调用路径，其键名是有限枚举、命名权归检索服务，fin 侧维护该枚举快照并做静态检查。理由是产品路径一旦带上关闭参数，「能力被关」与「能力失败」就不再可分，产品结果也无法与评测的「全开」配置对照。

Considered Options: 产品路径暴露能力开关（与降级信号混淆、产品与评测结果不可比）；产品路径按场景自动关能力（关的行为不可断言，违反静默失败清零）。
