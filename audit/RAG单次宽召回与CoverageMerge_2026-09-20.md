# LanShot RAG 单次宽召回与 Coverage Merge

日期：2026-09-20

目标：在百炼标准版知识库 1 QPS 约束下，不增加远程检索次数，不自建向量库，用一次远程宽召回 + 本地确定性分组重排提高多子问题覆盖率。

## 设计

原链路：

面试问题
→ route_question
→ 1 个综合 Query
→ 百炼 Search
→ 本地 Top3
→ LLM

新链路：

面试问题
→ route_question
→ retrieval_groups 按子问题生成事实组
→ broad_retrieval_query 把多个独立事实组压成 1 个远程 Query
→ 百炼 Search 仍然只调用 1 次
→ search_candidates 保留服务返回的宽候选，代码上最多接收 20 条 / 30000 字符
→ filter_knowledge 先去重/跨项目过滤
→ Coverage Merge：优先给每个事实组找一条直接证据
→ provider rank 补齐剩余槽位
→ 最终最多 7 个 evidence，最多 9000 字符
→ DeepSeek

没有新增第二次百炼请求，因此不会因为分组检索额外消耗知识库 QPS。

## 分组示例

第 10 题 EnterpriseRAG 会拆成：

- workflow
- chunking
- retrieval
- embedding
- metrics
- matryoshka

第 09 题 CODA Retry 会拆成：

- transport_retry
- stagnation
- plan_failure
- team_retry

第 06 题 CODA Memory 会拆成：

- memory_runtime
- memory_trust

分组只用于检索与证据选择，不额外调用 LLM。

## 本地 Coverage 判定

每组有普通 terms 和 strong_terms。

例如 chunking：
- 普通：1200 / 200 / chunk / overlap / 10722 / 115406
- 强证据：1200 / 200 / overlap / 115406

只有命中泛词 chunk 不再算“切块参数已覆盖”。

Matryoshka：
- 强证据：Matryoshka / MRL
- 只出现 embedding 不算覆盖。

这样 audit 中的 covered_groups 表示“有直接证据”，不是简单关键词沾边。

## 当前百炼服务侧实测

2026-09-20 已完成云端修改并发布受控版本：

- version 1：原正式版本，`rerank_top_n=5`，`qwen3-rerank`。
- version 2：曾短暂发布 `rerank_top_n=15`，但因为 beta 中已有未发布的 `qwen3-rerank-hybrid` 改动，存在实验污染，不作为结论依据。
- version 3：当前正式版本；完全复制 version 1，只把服务级和知识库级 `rerank_top_n` 从 5 改为 15，reranker 保持 `qwen3-rerank`。

真实 Search API 已验证 `received_hits=15`。LanShot 本地不会把 15 条全部送给模型，而是 Coverage Merge 后保留最多 5~7 条、总上下文最多 9000 字符。因此每题仍只消耗 1 次远程 Search，不增加 QPS。

## 当前坏例回放

### 06 - 长短期记忆

Top15 后：
- `received_hits=15`
- memory_runtime：覆盖到 Context / MemoryManager 相关资料
- memory_trust：覆盖到 save_memory 信任边界资料

但把 15 条原始候选展开后，仍然没有出现 SQLite ManagedMemoryStore、UNVERIFIED/VERIFIED 默认检索隔离、词项匹配而非向量检索这些精确事实卡。因此 06 的主要问题已经可以确认是语料缺口，而不是 TopK 不够。当前生成回答仍会因证据不足而保守，单轮 judge 为 48 分，不把这个低分归因于 Top15 本身。

### 09 - Retry

Top15 后：
- `received_hits=15`
- plan_failure：覆盖
- team_retry：覆盖
- stagnation：有相关停滞资料
- transport_retry：仍缺

把 15 条原始候选全部展开后，没有任何 RetryingLlmClient / max_attempts / base_delay_seconds 的精确参数卡进入 Top15，因此 09 的 Transport Retry 已经可以确认是语料缺口，而不是 TopK 不够。Coverage audit 会继续把 `transport_retry` 标记到 missing_groups，生成模型不再编造 2~3 次之类替代数字。当前单轮 judge 为 66 分，主要扣分仍来自缺少 max_attempts=3、0.25/0.5 秒、无 jitter 等源码事实。

### 10 - EnterpriseRAG

Top15 后：
- `received_hits=15`
- workflow：覆盖
- chunking：覆盖，召回 1200 / 200 基线证据
- retrieval：覆盖
- embedding：覆盖
- metrics：覆盖
- matryoshka：覆盖，召回 MRL / 2048 维兼容约束证据

因此第 10 题从原来的 4/6 提升到 6/6。最终回答能够正确说出 1200/200、98.09% 的指标口径、Qwen3 的净贡献和 MRL/2048 的边界。受控 version 3 下单轮生成 TTFT 约 1.31 秒、完整约 4.66 秒。Gemini judge 该轮连接失败；此前同样 Top15 但混入 hybrid reranker 的 version 2 得到 87 分，因此 87 不能作为“仅 Top15”的受控分数。

## 测试

新增/保留测试覆盖：

- 普通 search 继续遵守原 max_hits。
- search_candidates 可以保留服务返回的更宽候选。
- EnterpriseRAG 多子问题能构建 6 个 retrieval groups。
- 较低 provider rank 的直接 chunking / Matryoshka evidence 会被 Coverage Merge 保留。
- 只有泛词 chunk / embedding 不会虚报 group covered。
- 原有路由、RAG、语音提交流程继续回归。

当前相关测试：69 个通过。

## 当前未完成项

1. 06 需要补一张经过源码核验的 Memory 精确事实卡：MemoryManager、SQLite ManagedMemoryStore、UNVERIFIED/VERIFIED、默认检索过滤与词项评分机制。
2. 09 需要补一张 Transport Retry 精确事实卡：`max_attempts=3`（含首次请求）、默认 0.25/0.5 秒、Retry-After 上限、无 jitter，并与参数修正 / Plan Replan / Team review retry 分开。
3. 06 的 retrieval groups 还应继续细分成 context / store / trust / search 四组，避免“碰到 MemoryManager”就把整个 Memory 题判为已覆盖。
4. 在补完上述语料后，再跑完整 12 题，验证 Top15 + Coverage Merge 的整体收益和延迟分布。
