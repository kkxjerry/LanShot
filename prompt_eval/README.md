# LanShot 12 题 Prompt 回归

目标：用“核心问题模型答卷”的 12 道题做固定回归，分别检查路由、RAG 和最终回答。不要把三类问题混成“Prompt 效果”。

## 文件

- interview_12_cases.json：12 题测试集。每题包含 must_cover 与 must_not_claim。
- benchmark_prompt.py：复跑脚本，支持路由、检索、生成、A/B 与 DeepSeek 官方端点。
- voice_question_prompt_deepseek_v1.txt：第一版候选。
- voice_question_prompt_deepseek_v2.txt：增加“只用当前问题直接相关证据”和输出预算。
- voice_question_prompt_deepseek_v3.txt：增加项目事实与通用设计、不同 Retry 层级的区分。
- voice_question_prompt_deepseek_v4.txt：上一版稳定候选。
- voice_question_prompt_deepseek_v5.txt：当前生产候选。增加本轮作答槽位、Coverage 证据槽位，以及比较题、性能题、评测题、高压交付和分布式并发的完整性规则。

生产中的 `lanshot2/voice_question_prompt.txt` 已同步 v5。

## 三层测试

第一层只测路由：

python3 prompt_eval/benchmark_prompt.py --route-only

目标是通用题不被错误送进项目 RAG，CODA / EnterpriseRAG 项目题能正确带项目锚点。

第二层只测检索：

python3 prompt_eval/benchmark_prompt.py --retrieval-only --ids 06,07,08,09,10

检查 Top3 是否真正提供本题所需事实。没有召回的事实，不应该要求生成模型靠 Prompt 猜出来。

第三层测生成：

python3 prompt_eval/benchmark_prompt.py \
  --provider bailian \
  --model deepseek-v4.1-flash \
  --candidate-prompt prompt_eval/voice_question_prompt_deepseek_v5.txt \
  --variant candidate \
  --thinking off \
  --skip-judge

接入 DeepSeek 官方 API 后：

python3 prompt_eval/benchmark_prompt.py \
  --provider deepseek \
  --model deepseek-flash \
  --candidate-prompt prompt_eval/voice_question_prompt_deepseek_v5.txt \
  --variant candidate \
  --thinking off

官方 key 可放环境变量 DEEPSEEK_API_KEY，或 macOS Keychain：
service=com.lanshot.deepseek
account=DEEPSEEK_API_KEY

## 当前结果

路由最初 06、07、08、09、12 共 5 题存在项目归属问题。修正后 12/12 与测试标签一致。

相关自动测试：
- lanshot_common.test_interview_rag + lanshot_common.test_knowledge：39 个测试通过。
- lanshot2/test_lanshot2.py：25 个测试通过。

第 01 题的 A/B 裁判结果：原 Prompt 72，v4 为 79。v4 的优势主要来自把 Function Calling 与 ReAct 的关系、组合方式和微调边界说得更清楚。

第 02 题：v3 会尝试补字符串和 Unicode 转义，存在“格式修复替代内容恢复”的风险。v4 + non-thinking 后改成只恢复确定结构，字符串内容不完整时重新生成对应字段，约 168 字。

第 03 题：v3 会把最终决策写成不可变。v4 改成当前有效值与历史版本分离，约 217 字。

第 07 题：v4 + non-thinking 约 204 字，能明确说明 multi_edit 是整批预检、逐文件写入，单文件 atomic write 不等于跨文件事务，expected_sha256 仍有检查到提交之间的竞态边界。

第 08 题：回答能守住单文件原子、版本检查和 OS sandbox 边界，但 Top3 RAG 中混有 Plan/Team 任务并发与 ToolRegistry 单轮并发，模型仍可能把两层串起来。这里优先修 RAG 证据，不继续堆 Prompt。

第 09 题：Prompt 已避免编造固定重试次数，也不再把所有失败写成“固定两次重试”。但当前 Top3 RAG 没有给出 RetryingLlmClient 的 max_attempts=3、base_delay=0.25 等源码事实，所以只能保守说明次数未确认。该问题属于知识证据缺口。

第 10 题：v4 能正确说明 Hit@10=98.09% 不是答案准确率，也不会把 Qwen3 的总贡献夸大；但单次综合 Query 的 Top3 仍没召回 1200/200 切块参数。专门查询 “EnterpriseRAG 1200 200 chunk overlap retriever_k 50 rrf_k 10” 可以检索到参数卡，说明后续更适合做多子问题检索。

第 11 题：v4 补齐了分布式锁过期、唯一约束与引用计数的作用边界，不再把“锁 + 唯一索引”说成全系统原子一致。

## Thinking 结果

当前实测是百炼上的 deepseek-v4.1-flash，不是 DeepSeek 官方端点，因此只作为接入前参考。

开启 low thinking 时，复杂题首字经常在十几到三十秒，且第 12 题出现过达到输出上限而失败。

关闭 thinking 后：
- 第 07 题：首字约 1.0 秒，完整约 2.9 秒，204 字。
- 第 10 题：首字约 1.6 秒，完整约 5.5 秒；另一轮约 1.2 / 4.2 秒。
- 第 12 题：首字约 1.1 秒，完整约 4.3 秒，303 字。

单轮结果不能当稳定延迟 SLA，但对实时面试场景已经足以说明：non-thinking 应优先作为默认档，low thinking 留给手动难题增强，再在 DeepSeek 官方端点重复验证。

## 当前推荐

系统 Prompt 使用 v5，动态输出预算和内部覆盖 checklist 由 `lanshot_common/interview_rag.py` 的 `generation_prompt` 注入。Checklist 同时包含面试官明确子问题和 Coverage Merge 已拿到的独立证据主题，不增加额外 LLM 调用。

不要把 12 题正确答案直接写进 System Prompt。System Prompt 负责回答纪律；项目事实由 RAG 提供。否则 Prompt 会越来越长，而且代码或实验版本变化后会迅速过期。

正式切换 DeepSeek 官方 API 后，固定同一 RAG、同一 12 题，至少比较 thinking=disabled 和 low 两档，并记录事实正确、边界错误、子问题覆盖、口语可读性、字数、首字延迟、完整耗时和 RAG 命中。

## 百炼多模型矩阵

新增 `benchmark_bailian_deepseek_matrix.py`，直接读取百炼当前账号可见模型，并在同一批 RAG 证据上比较多个 DeepSeek 版本。

完整记录见：`百炼DeepSeek多模型矩阵_2026-09-20.md`。

当前 12 题最终候选结果：

- `deepseek-v4.1-flash + thinking off`：平均 77.83，平均完整耗时约 3.82 秒。
- `deepseek-v4-pro-0813 + thinking off`：平均 77.58，平均完整耗时约 4.51 秒。

当前生产已切换为 `deepseek-v4.1-flash + thinking off + Prompt v5`。v5 固定 12 题三轮总体均分 90.72；生产 `submit_snapshot()` 完整 12 题主链单轮为 95.67，12/12 成功。完整记录见 `v5生产接线与主链回归_2026-09-20.md`。