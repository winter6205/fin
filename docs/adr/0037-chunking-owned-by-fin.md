# 0037. Chunking belongs to fin; the retrieval service must not re-split

Date: 2026-08-31
Status: accepted

解析与切分（含父子分块）的唯一权威是 fin：fin 推送的块文本即最终文本，检索服务按原样存储与召回，不做任何形式的二次切分、截断、重组或归一化改写。理由是 **citation verifier** 要求答案片段是 fin 冻结单元文本的连续子串——由检索侧按字符硬切会切断这一子串关系，真实引用会被判成凭空生成；排序、融合、重排仍留在 fin 之外。ADR 0030 与 ADR 0032 中「切分归检索服务 / 切分留在 fin 之外」的条款由本 ADR 改判，两条 ADR 的其余决定不变（两者已有的 superseded 状态也不因此变更）。

Considered Options: 切分留在检索服务（引用门不成立，子串关系被切断）；两侧各切一份（两份切分权威、子串关系不可断言）。
