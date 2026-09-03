# 0043. Raw filing bytes live in an object store; locators stay in fin

Date: 2026-09-02
Status: accepted

原始申报（及后续多格式）字节进 MinIO/S3 兼容 **object store**；Postgres 只存元数据与指针；可引用的 extracted 树仍在 fin。Git 在配置了 object store 之后不再当字节仓库。理由是申报 PDF/HTML 体积会撑破仓库与 checkpointer，而 **locator** / **citation verifier** 比对的是冻结单元文本，不是 object key。本决定承接 2026-08-29 `corpus-ops-depth` 假设门第 5 条；不改 ADR 0037（切分归 fin）。未配置 store URL 时必须 typed 降级或文档化本地回退，禁止静默丢字节。

Considered Options: 把原始字节进 Postgres bytea（库膨胀、备份与 cite 树耦合）；继续用 git 当仓库（评测夹具可，产品库不可）；object key 当 locator（引用门失去冻结文本基准）。
