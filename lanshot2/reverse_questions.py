from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class ReverseQuestion:
    priority: str          # P0, P1, P2, P3
    category: str          # 追问弥补, 观点探针, 业务探针, 冲突求证, 复盘备选
    topic: str             # 主题，如 "Hybrid Retrieval 融合策略"
    question: str          # 高情商反问话术
    context_reason: str    # 为什么问这个（命中哪个追问链或回答漏洞）
    score: float           # 综合优先级评分 (0.0 ~ 10.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REVERSE_QUESTION_SYSTEM_PROMPT = """你是一位资深技术专家兼面试复盘教练。当前技术面试已进入尾声的“候选人反问”环节。
你的任务是根据整场面试的真实对话记录，为候选人提炼出最有价值的反问问题候选池，并按优先级（P0~P3）严格排序。

【反问问题产生维度与优先级】：
- P0（必杀级）：【我没答好 + 面试官连续追问（>=2次）】。弥补当场失分印象，挖掘对方线上最核心的工程权衡（Trade-off）。
- P1（真知级）：【面试官提出了不同观点或踩坑，但未展开】。套取大厂/团队一线踩坑得出的真实生产经验。
- P2（业务级）：【明显带有对方公司业务特征的架构/高并发/大数据题】。探测该团队当前真实的技术债务与业务挑战。
- P3（备选级）：【其他有回答缺口、或值得探讨的技术点】。作为后备问题，供面试官答得快时继续跟进，同时作为战后复盘清单。

【话术要求】：
1. 姿态谦逊、专业、探讨性质，以“刚才您提到/问到……”自然承接；
2. 绝对不能让面试官感觉被挑战或被反考，必须是抱着“向一线生产专家请教实践”的姿态；
3. 聚焦在工程取舍（Trade-off）、线上真实瓶颈、边界场景与踩坑经验。

【输出格式】：
请仅输出严格的 JSON 数组（不要输出任何额外 markdown 标记或解释）：
[
  {
    "priority": "P0",
    "category": "追问弥补",
    "topic": "多路召回融合策略",
    "question": "刚才您连续问了 RRF 和 weighted fusion，我感觉我当时回答得还不够落地。想请教一下，你们实际做多路召回融合时，是更偏 rank fusion，还是会做 score calibration 归一化？实际落地时有哪些反直觉的坑吗？",
    "context_reason": "面试官追问了4轮RRF，候选人未给出实验对比和score不可比解释",
    "score": 9.5
  }
]
"""


def parse_reverse_questions_from_response(raw_text: str) -> list[dict[str, Any]]:
    """Extracts and parses JSON array from LLM response."""
    text = raw_text.strip()
    match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict) and "question" in item]
    except json.JSONDecodeError:
        pass
    return []


def heuristic_reverse_questions(dialogue_text: str) -> list[ReverseQuestion]:
    """Fallback rule-based reverse question extractor when LLM is unavailable."""
    candidates: list[ReverseQuestion] = []
    lines = [line.strip() for line in dialogue_text.splitlines() if line.strip()]

    # Extract interviewer questions
    interviewer_questions: list[str] = []
    for line in lines:
        if "【面试官】" in line or line.startswith("系统声音识别"):
            content = re.sub(r"^【面试官】(\s*\(\d{2}:\d{2}:\d{2}\))?:?\s*", "", line)
            if len(content) >= 6 and any(kw in content for kw in ("为什么", "怎么", "如何", "那", "架构", "设计", "瓶颈", "一致性")):
                interviewer_questions.append(content)

    if not interviewer_questions:
        return [
            ReverseQuestion(
                priority="P1",
                category="业务探针",
                topic="团队核心挑战与技术演进",
                question="想请教一下，贵团队目前在业务演进过程中，面临的最大技术痛点或正在重构的模块是什么？",
                context_reason="了解团队当前真实技术债务与核心挑战",
                score=8.0,
            ),
            ReverseQuestion(
                priority="P2",
                category="工程取舍",
                topic="生产环境架构选型",
                question="针对刚才讨论的业务场景，想请教下贵团队在生产环境中做选型时，最看重的权衡指标（如成本、延迟、一致性）通常是如何排序的？",
                context_reason="探寻大厂一线生产环境的真实权衡标准",
                score=7.0,
            ),
        ]

    # Generate P0 / P1 from actual interviewer questions
    for idx, q in enumerate(interviewer_questions[-3:]):
        topic = q[:25] + ("..." if len(q) > 25 else "")
        priority = "P0" if idx == 0 else ("P1" if idx == 1 else "P2")
        score = 9.0 - idx * 0.8
        candidates.append(
            ReverseQuestion(
                priority=priority,
                category="追问弥补" if priority == "P0" else "观点探针",
                topic=topic,
                question=f"刚才您问到了“{topic}”，这个场景很有挑战性。想请教一下，贵团队目前在实际生产项目中，主要是采取什么方案做权衡的？有没有踩过什么反直觉的坑？",
                context_reason=f"结合刚才讨论的问题：{q}",
                score=score,
            )
        )

    # Add a team inquiry as P3
    candidates.append(
        ReverseQuestion(
            priority="P3",
            category="复盘备选",
            topic="业务团队技术规划",
            question="想请教一下，这个岗位所在的团队，目前业务和技术重构的时间精力占比大概是怎样的？",
            context_reason="了解团队日常工作节奏与技术氛围",
            score=6.0,
        )
    )

    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates


def format_reverse_questions_display(questions: list[ReverseQuestion | dict[str, Any]]) -> str:
    """Formats questions into a clear, numbered Markdown document for overlay display."""
    if not questions:
        return "（未提取到有效的反问建议）"

    blocks = ["💡【反问候选池（按优先级排序）】\n"]
    for idx, item in enumerate(questions, 1):
        if isinstance(item, dict):
            p = item.get("priority", "P2")
            cat = item.get("category", "反问建议")
            topic = item.get("topic", "")
            q = item.get("question", "")
            reason = item.get("context_reason", "")
        else:
            p = item.priority
            cat = item.category
            topic = item.topic
            q = item.question
            reason = item.context_reason

        blocks.append(
            f"━━━━━━━━━━━━━━ [{idx}/{len(questions)}] 优先级: {p} · {cat} ━━━━━━━━━━━━━━\n"
            f"🎯 主题：{topic}\n"
            f"🗣️ 推荐反问：\n“{q}”\n\n"
            f"📌 提问理由：{reason}\n"
        )
    return "\n".join(blocks)
