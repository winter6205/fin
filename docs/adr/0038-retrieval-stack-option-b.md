# 0038. Retrieval stack is option B: graph main path, Postgres vector extension in place, Elasticsearch precision layer

Date: 2026-08-31
Status: accepted

检索栈定档 B：找实体（向量 + 精度层）→ 图多跳主检索 → 图上未找到实体时回退（向量 + 精度层）；向量是同一个能力在首尾两个环节被调用，不是两种角色，精度层（Elasticsearch，中文分词 / Okapi BM25 / 短语查询 / 字段加权）与向量互补、不做主备切换。Postgres 保持权威源并以向量扩展原地加索引、不搬迁；向量索引与词法索引都是派生索引，可删除、可从权威源重灌，重灌不影响权威数据，因而向量库可平滑升级而不动权威数据；RRF 与重排开启。fin 侧据此承担「任意时点都能用同一规则版本与参数表版本从权威源重建全部推送内容」的义务。

Considered Options: 精度层落在 Postgres 侧——只换文本配置不产生切分能力、中文分词扩展会引入第二套排序口径并绑定数据库实例、N-gram 方案破坏 Okapi BM25 的词级语义，三条逐一排除；搬迁到独立向量库——权威源与派生索引分离后无必要。
