import json
import unittest
from types import SimpleNamespace
from unittest import mock

from lanshot_common.interview_rag import broad_retrieval_query, retrieval_groups, route_question, filter_knowledge, clean_turn, generation_prompt, coda_source_names
from lanshot_common.knowledge import KnowledgeHit, KnowledgeSearchResult
from lanshot_common.bailian_stream import ask_stream


class RoutingTests(unittest.TestCase):
    def test_generic_multiagent_is_not_coda(self):
        route = route_question("什么任务适合单 Agent，什么任务适合多 Agent？", "")
        self.assertEqual(route.kind, "general")
        self.assertFalse(route.use_knowledge)
        self.assertNotIn("ReAct Plan Team", route.query)

    def test_project_multiagent_retains_expansion(self):
        route = route_question("CODA 为什么要同时做单 Agent 和多 Agent？", "")
        self.assertEqual(route.project, "coda")
        self.assertTrue(route.use_knowledge)
        self.assertIn("ReAct Plan Team", route.query)

    def test_personal_agent_question_is_anchored_to_coda(self):
        route = route_question("你这个 Agent 的长短期记忆是怎么设计的？", "")
        self.assertEqual(route.project, "coda")
        self.assertTrue(route.use_knowledge)
        self.assertIn("Memory Context", route.query)

    def test_personal_tool_conflict_question_is_anchored_to_coda(self):
        route = route_question(
            "模型一次返回多个工具调用怎么处理？你们有没有互斥和文件版本检查？",
            "",
        )
        self.assertEqual(route.project, "coda")
        self.assertTrue(route.use_knowledge)

    def test_project_specific_continuation_inherits_coda(self):
        route = route_question(
            "失败重试机制怎么设计？DAG子任务失败以后怎么办？",
            "",
            previous_question="你做的 CODA 里多个工具怎么处理？",
        )
        self.assertEqual(route.project, "coda")
        self.assertTrue(route.previous)
        self.assertTrue(route.use_knowledge)

    def test_generic_agent_question_is_still_general(self):
        route = route_question("Agent 的评测集应该怎么设计？", "")
        self.assertEqual(route.kind, "general")
        self.assertEqual(route.project, "")
        self.assertFalse(route.use_knowledge)

    def test_same_question_reaches_retrieval_and_generation(self):
        route = route_question("第3题，CODA怎么做规划？失败后怎么办？", "不知道")
        self.assertIn("怎么做规划？失败后怎么办？", route.query)
        self.assertIn(route.current, route.generation_input())
        self.assertNotIn("第3题", route.generation_input())

    def test_explicit_project_switch_does_not_inherit(self):
        route = route_question("这个 EnterpriseRAG 项目的98.09%代表什么？", "",
                               previous_question="CODA 怎么规划？")
        self.assertEqual(route.project, "enterprise_rag")
        self.assertEqual(route.previous, "")
        self.assertNotIn("CODA", route.query)

    def test_generic_hnsw_after_coda_is_not_personal(self):
        route = route_question("那 HNSW 是按一个语义维度建立索引的吗？", "",
                               previous_question="CODA 的 Plan 怎么运行？")
        self.assertEqual(route.kind, "general")
        self.assertEqual(route.previous, "")

    def test_followup_inherits_only_original_question(self):
        previous = "系统声音识别：\nCODA 的 Plan 怎么运行？\n\n麦克风识别：\n服务了一百万用户"
        route = route_question("那第三个任务失败以后呢？", "", previous_question=previous)
        self.assertEqual(route.project, "coda")
        self.assertIn("CODA", route.previous)
        self.assertNotIn("一百万", route.query)
        self.assertNotIn("一百万", route.generation_input())

    def test_project_followup_query_prioritizes_current_failure(self):
        route = route_question("那第三个任务失败以后怎么恢复？", "",
                               previous_question="CODA 的 Plan 模式怎么管理任务和依赖？")
        self.assertTrue(route.query.startswith("那第三个任务失败以后怎么恢复？"))
        self.assertIn("CODA PaiCLI", route.query)
        self.assertIn("Resume", route.query)
        self.assertNotIn("怎么管理任务和依赖", route.query)
        self.assertIn("怎么管理任务和依赖", route.previous)

    def test_explicit_topic_change_clears_previous(self):
        route = route_question("换个问题，Redis持久化怎么做？", "",
                               previous_question="CODA 的上下文怎么压缩？")
        self.assertEqual(route.kind, "general")
        self.assertNotIn("CODA", route.query)

    def test_personal_numbers_always_need_evidence(self):
        route = route_question("你的项目服务了多少用户？", "")
        self.assertTrue(route.use_knowledge)
        self.assertEqual(route.kind, "personal")

    def test_unknown_question_does_not_blindly_skip_knowledge(self):
        self.assertTrue(route_question("请谈谈最难的挑战", "").use_knowledge)

    def test_microphone_does_not_override_valid_interviewer(self):
        route = route_question("请解释进程和线程", "CODA 的 Team 怎么做")
        self.assertEqual(route.kind, "general")
        self.assertNotIn("CODA", route.query)

    def test_microphone_fallback_when_system_empty(self):
        route = route_question("", "CODA怎么停止重复读取？")
        self.assertEqual(route.project, "coda")

    def test_empty_and_filler_are_not_questions(self):
        for text in ("", "嗯", "好的", "第8题。"):
            with self.subTest(text=text):
                self.assertEqual(route_question(text, "").kind, "incomplete")

    def test_coda_rag_is_not_enterprise_rag(self):
        route = route_question("CODA项目内部的RAG工具怎么实现？", "")
        self.assertEqual(route.project, "coda")

    def test_input_budget_and_exact_adjacent_dedup(self):
        self.assertEqual(clean_turn("怎么做？怎么做？为什么？"), "怎么做？为什么？")
        self.assertLessEqual(len(route_question("长" * 5000, "").current), 1800)

    def test_cross_project_and_exact_duplicates_are_removed(self):
        result = KnowledgeSearchResult("hit", "CODA", hits=(
            KnowledgeHit("事实A", .9, "CODA_规划.md"),
            KnowledgeHit("事实A", .8, "CODA_问答.md"),
            KnowledgeHit("事实B", .95, "EnterpriseRAG_评测.md"),
            KnowledgeHit("其他事实", .7, "无明确项目名.md"),
        ))
        filtered, audit = filter_knowledge(result, route_question("CODA怎么规划？", ""))
        self.assertEqual([x.text for x in filtered.hits], ["事实A", "其他事实"])
        self.assertEqual(len(audit["rejected"]), 2)
        self.assertEqual(filtered.hits[0].score, .9)

    def test_unqualified_coda_filenames_are_not_enterprise_evidence(self):
        result = KnowledgeSearchResult("hit", "EnterpriseRAG", hits=(
            KnowledgeHit("权限", .95, "12_Capability与ToolScope的双重约束"),
            KnowledgeHit("介绍", .9, "02_三种时长的项目介绍.md"),
        ))
        filtered, audit = filter_knowledge(result, route_question("EnterpriseRAG的指标是什么？", ""))
        self.assertEqual(filtered.status, "empty")
        self.assertEqual(len(audit["rejected"]), 2)
        self.assertEqual(len(coda_source_names()), 68)

    def test_fact_budget_does_not_force_mechanism_question_short(self):
        fact = generation_prompt("基础提示", route_question("98.09%是答案准确率吗？", ""))
        deep = generation_prompt("基础提示", route_question("CODA怎么恢复？是不是所有失败都能重跑？", ""))
        self.assertIn("硬上限180", fact)
        self.assertIn("每个子问题用1到2句回答", deep)
        self.assertNotIn("硬上限180", deep)

    def test_generation_prompt_adds_internal_question_and_evidence_checklist(self):
        route = route_question(
            "CODA最大工程难点是什么？多文件修改怎么防止改到一半出错？",
            "",
        )
        prompt = generation_prompt(
            "基础提示",
            route,
            coverage={
                "group_assignments": {
                    "planning": {
                        "covered": True,
                        "matched_terms": ["Planner", "DAG", "ReAct"],
                    },
                    "multi_edit": {
                        "covered": True,
                        "matched_terms": ["multi_edit", "FileMutation"],
                    },
                }
            },
        )
        self.assertIn("本轮内部覆盖检查", prompt)
        self.assertIn("面试官本轮明确问点", prompt)
        self.assertIn("Planner、DAG校验调度与ReAct分工", prompt)
        self.assertIn("多文件编辑提交与部分成功边界", prompt)
        self.assertIn("不得原样输出", prompt)

    def test_filter_does_not_rewrite_numeric_facts(self):
        text = "Hit@10=98.09%，不是答案准确率。"
        result = KnowledgeSearchResult("hit", "RAG", hits=(KnowledgeHit(text, .9, "EnterpriseRAG.md"),))
        filtered, _ = filter_knowledge(result, route_question("RAG项目指标是什么？", ""))
        self.assertEqual(filtered.hits[0].text, text)

    def test_enterprise_multi_question_builds_coverage_groups_and_broad_query(self):
        route = route_question(
            "EnterpriseRAG端到端流程是什么？切块多大？几路检索？为什么用Qwen3？98.09%是什么？Matryoshka是什么？",
            "",
        )
        names = [group.name for group in retrieval_groups(route)]
        self.assertEqual(
            names,
            ["workflow", "chunking", "retrieval", "embedding", "metrics", "matryoshka"],
        )
        broad = broad_retrieval_query(route)
        self.assertIn("1200", broad)
        self.assertIn("All-Gold@10", broad)
        self.assertIn("Matryoshka", broad)
        self.assertIn("相互独立的事实主题", broad)

    def test_coverage_merge_keeps_lower_ranked_direct_evidence(self):
        route = route_question(
            "EnterpriseRAG端到端流程是什么？切块多大？几路检索？为什么用Qwen3？98.09%是什么？Matryoshka是什么？",
            "",
        )
        result = KnowledgeSearchResult("hit", broad_retrieval_query(route), hits=(
            KnowledgeHit("项目主线 Document Evidence Prompt Answer", .95, "主线1.md"),
            KnowledgeHit("项目主线 EvidenceBuilder Query-Spans Packing", .94, "主线2.md"),
            KnowledgeHit("四路 Dense BM25 Keyword BM25 English BM25 Weighted RRF", .93, "检索.md"),
            KnowledgeHit("Qwen3-Embedding-4B E5 2048 95.96 97.87", .92, "向量.md"),
            KnowledgeHit("Hit@10 98.09 All-Gold@10 92.34 MRR 470", .91, "指标.md"),
            KnowledgeHit("主基线使用1200字符，overlap 200，115406 chunks", .70, "切块.md"),
            KnowledgeHit("Matryoshka MRL 支持维度截断", .69, "套娃.md"),
        ))
        filtered, audit = filter_knowledge(result, route)
        sources = [hit.document_name for hit in filtered.hits]

        self.assertIn("切块.md", sources)
        self.assertIn("套娃.md", sources)
        self.assertEqual(audit["covered_groups"], 6)
        self.assertGreaterEqual(audit["accepted_hits"], 5)

    def test_coverage_audit_does_not_count_generic_term_as_direct_evidence(self):
        route = route_question("EnterpriseRAG切块多大？Matryoshka是什么？", "")
        result = KnowledgeSearchResult("hit", broad_retrieval_query(route), hits=(
            KnowledgeHit("这个 chunk 使用 embedding 表示。", .9, "泛化.md"),
        ))
        _, audit = filter_knowledge(result, route)
        self.assertEqual(audit["covered_groups"], 0)
        self.assertFalse(audit["group_assignments"]["chunking"]["covered"])
        self.assertFalse(audit["group_assignments"]["matryoshka"]["covered"])

    def test_coverage_does_not_treat_metadata_keywords_as_body_evidence(self):
        route = route_question(
            "CODA失败重试机制怎么设计？网络抖动怎么重试？重试几次后停止？",
            "",
        )
        result = KnowledgeSearchResult("hit", broad_retrieval_query(route), hits=(
            KnowledgeHit(
                "【文档名】: retry\n【标题】: 概览\n【正文】: "
                "project: PaiCLI\nmodule: LlmRetry\n"
                "keywords: RetryingLlmClient max_attempts base_delay_seconds 0.25 0.5 jitter stagnation_window\n"
                "status: confirmed\n"
                "模型请求失败后会做有限重试，默认总共尝试3次。",
                .99,
                "retry-overview.md",
            ),
            KnowledgeHit(
                "【文档名】: retry\n【标题】: 默认重试参数\n【正文】: "
                "原材料的 max_attempts 为3，base_delay_seconds为0.25，"
                "两次等待是0.25秒和0.5秒，当前没有随机jitter，并处理Retry-After。",
                .80,
                "retry-details.md",
            ),
            KnowledgeHit(
                "AgentBudget 的 stagnation_window=3。第二次连续相同工具轮次先提醒，第三次仍重复则停止。",
                .79,
                "stagnation.md",
            ),
        ))
        filtered, audit = filter_knowledge(result, route)
        assignment = audit["group_assignments"]["transport_retry"]
        self.assertEqual(assignment["source"], "retry-details.md")
        self.assertIn("0.25", assignment["matched_terms"])
        self.assertIn("jitter", assignment["matched_terms"])
        self.assertIn("retry-details.md", [hit.document_name for hit in filtered.hits])

    def test_coverage_metadata_only_chunk_does_not_cover_numeric_fact(self):
        route = route_question("EnterpriseRAG切块多大？", "")
        result = KnowledgeSearchResult("hit", broad_retrieval_query(route), hits=(
            KnowledgeHit(
                "【文档名】: chunking\n【标题】: 概览\n【正文】: "
                "project: EnterpriseRAG\nmodule: Chunking\n"
                "keywords: 1200 200 overlap 115406\nstatus: confirmed\n"
                "项目使用固定切块策略。",
                .95,
                "chunking-overview.md",
            ),
        ))
        _, audit = filter_knowledge(result, route)
        self.assertFalse(audit["group_assignments"]["chunking"]["covered"])

    def test_filtered_empty_result_does_not_claim_hit(self):
        result = KnowledgeSearchResult("hit", "CODA", hits=(KnowledgeHit("B", .9, "EnterpriseRAG.md"),))
        filtered, _ = filter_knowledge(result, route_question("CODA怎么规划？", ""))
        self.assertEqual(filtered.status, "empty")


class Response:
    def __init__(self, chunks):
        self.body = "".join("data: " + json.dumps(x, ensure_ascii=False) + "\n\n" for x in chunks).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def __iter__(self):
        return iter(self.body.splitlines(keepends=True))


class StreamingTests(unittest.TestCase):
    def client(self, chunks):
        return SimpleNamespace(model="glm-5.3", url="https://example.invalid/chat/completions",
                               api_key="unit-test-only", opener=mock.Mock(return_value=Response(chunks)))

    def test_only_content_is_visible_and_usage_is_recorded(self):
        client = self.client([
            {"choices": [{"delta": {"reasoning_content": "隐藏的推理"}}]},
            {"choices": [{"delta": {"content": "第一句。"}}]},
            {"choices": [{"delta": {"content": "原因。"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"completion_tokens": 21}},
        ])
        updates = []
        answer = ask_stream(client, "问题", "提示", on_update=updates.append)
        self.assertEqual(answer, "第一句。原因。")
        self.assertEqual(updates[0], "第一句。")
        self.assertEqual(updates[-1], answer)
        self.assertNotIn("隐藏", str(updates))
        self.assertTrue(client.last_timing["streaming"])
        self.assertIsNotNone(client.last_timing["first_visible_ms"])
        self.assertEqual(client.last_usage["completion_tokens"], 21)
        payload = json.loads(client.opener.call_args.args[0].data)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_non_thinking_configuration_is_sent(self):
        client = self.client([
            {"choices": [{"delta": {"content": "直接答案"}, "finish_reason": "stop"}]}
        ])
        client.enable_thinking = False
        client.reasoning_effort = "low"
        answer = ask_stream(client, "问题", "提示")
        self.assertEqual(answer, "直接答案")
        payload = json.loads(client.opener.call_args.args[0].data)
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(client.last_timing["thinking_level"], "none")

    def test_truncated_answer_is_not_success(self):
        client = self.client([{"choices": [{"delta": {"content": "半句话"}, "finish_reason": "length"}]}])
        with self.assertRaisesRegex(RuntimeError, "输出上限"):
            ask_stream(client, "问题", "提示")

    def test_transport_eof_without_finish_is_not_success(self):
        client = self.client([{"choices": [{"delta": {"content": "半句话"}}]}])
        with self.assertRaisesRegex(RuntimeError, "完整答案"):
            ask_stream(client, "问题", "提示")

    def test_reasoning_only_is_not_answer(self):
        client = self.client([{"choices": [{"delta": {"reasoning_content": "隐藏"}, "finish_reason": "stop"}]}])
        with self.assertRaisesRegex(RuntimeError, "完整答案"):
            ask_stream(client, "问题", "提示")


if __name__ == "__main__":
    unittest.main()
