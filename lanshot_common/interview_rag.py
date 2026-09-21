"""Deterministic interview routing; no extra model call and no invented history.

Archive the original two-channel transcript separately. Retrieval and generation
share the same cleaned current turn. Previous model answers are not evidence.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass, replace

from lanshot_common.knowledge import KnowledgeHit, KnowledgeSearchResult

_QUESTION = re.compile(r"第\s*(?:[0-9]+|[一二三四五六七八九十百]+)\s*题\s*[，,:：。.!！?？]*\s*")
_CODA = re.compile(r"(?<![A-Za-z0-9])(?:codah?|paicli)(?![A-Za-z0-9])", re.I)
_ENTERPRISE = re.compile(r"Enterprise\s*RAG|minirag|企业(?:级)?\s*RAG", re.I)
_PERSONAL = re.compile(r"你的|你这个|你们的|你们有|你做的|你做这个|你开发的|你实际|你在实际开发|你之前|你当时|我的|我们(?:的|在)|个人贡献|负责|实习|简历|项目")
_CODA_PERSONAL_TOPIC = re.compile(r"Coding\s*Agent|\bAgent\b|DAG|Plan|ReAct|Worker|Reviewer|Planner|ToolRegistry|save_memory|工具调用|文件写|多文件|长短期记忆|长期记忆|失败重试|熔断", re.I)
_PROJECT_CONTINUATION = re.compile(r"(?:DAG|Plan|Worker|Reviewer|Planner|ToolRegistry|save_memory|expected_sha256).{0,36}(?:怎么|为什么|失败|恢复|重试|冲突|实现|怎么办)", re.I)
_FOLLOWUP = re.compile(r"^(?:那[，, ]*)?(?:这个|这种|这些|它|刚才|上面|前面|你说的|你提到的|你接入的|为什么是\s*[0-9一二三四五六七八九十百]|那么|那为什么|那怎么)|(?:刚才|你说的|你提到的)", re.I)
_SWITCH = re.compile(r"换(?:个|一个|一道)问题|下(?:个|一个|一道)问题|接下来(?:问|聊)|另外问")
_STANDALONE = re.compile(r"HNSW|MySQL|Redis|GIL|TCP|HTTP|Linux|JVM|垃圾回收|进程和线程|进程与线程", re.I)
_GENERAL = re.compile(r"什么是|解释|区别|原理|适合|如何|怎么|算法|比较|SQL|为什么|有哪些", re.I)
_FOCUS = re.compile(r"^(?:嗯|啊|哦|好的|好|对|是的|谢谢|继续)[。.!！?？,， ]*$")
MAX_TURN_CHARS = 1800
MAX_PREVIOUS_CHARS = 900
MAX_MICROPHONE_CHARS = 1000
MAX_FINAL_EVIDENCE_HITS = 7
MAX_FINAL_EVIDENCE_CHARS = 9_000


def clean_turn(value: str, limit: int = MAX_TURN_CHARS) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    markers = list(_QUESTION.finditer(value))
    if markers:
        value = value[markers[-1].end():].strip()
    switches = list(_SWITCH.finditer(value))
    if switches:
        value = value[switches[-1].end():].lstrip("，,:：。 ")
    # Remove only adjacent exact duplicate sentences, never distinct subquestions.
    pieces = re.findall(r"[^。！？!?]+[。！？!?]?", value)
    deduped: list[str] = []
    for piece in pieces:
        if not deduped or piece.strip() != deduped[-1].strip():
            deduped.append(piece)
    value = "".join(deduped).strip(" ，,:：。.!！")
    return value[-limit:]


def interviewer_from_archive(value: str) -> str:
    if "系统声音识别：" in value:
        value = value.split("系统声音识别：", 1)[1]
    return clean_turn(value.split("麦克风识别：", 1)[0], MAX_PREVIOUS_CHARS)


def project_of(value: str) -> str:
    coda = bool(_CODA.search(value))
    enterprise = bool(_ENTERPRISE.search(value)) or (
        not coda and bool(re.search(r"\bRAG\b", value, re.I)) and bool(_PERSONAL.search(value))
    )
    if coda and enterprise:
        return "mixed"
    if coda:
        return "coda"
    if enterprise:
        return "enterprise_rag"
    return ""


@dataclass(frozen=True)
class QuestionRoute:
    kind: str
    project: str
    current: str
    query: str
    previous: str = ""
    microphone: str = ""
    reason: str = ""

    @property
    def use_knowledge(self) -> bool:
        return self.kind not in ("general", "incomplete") and bool(self.query)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "project": self.project, "reason": self.reason,
            "followup": bool(self.previous), "use_knowledge": self.use_knowledge,
            "current_question": self.current, "query": self.query,
            "previous_question": self.previous,
        }

    def generation_input(self) -> str:
        parts = ["系统声音识别：\n" + self.current,
                 "麦克风识别：\n" + (self.microphone or "（未识别到内容）")]
        if self.previous:
            parts.append("上一轮面试官原问题（仅解释指代，不要重新回答）：\n" + self.previous)
        parts.append("本轮作答目标：\n" + self.current)
        return "\n\n".join(parts)


def route_question(system_text: str, microphone_text: str, *, previous_question: str = "") -> QuestionRoute:
    interviewer = clean_turn(system_text)
    microphone = clean_turn(microphone_text, MAX_MICROPHONE_CHARS)
    current = interviewer if len(interviewer) >= 4 and not _FOCUS.fullmatch(interviewer) else microphone
    if not current or _FOCUS.fullmatch(current):
        return QuestionRoute("incomplete", "", current, current, reason="no_question")
    project = project_of(current)
    personal = bool(_PERSONAL.search(current))
    if not project and personal and _CODA_PERSONAL_TOPIC.search(current):
        project = "coda"
    prior = interviewer_from_archive(previous_question) if previous_question else ""
    continuation = bool(_PROJECT_CONTINUATION.search(current))
    followup = bool(
        prior
        and (_FOLLOWUP.search(current) or current.startswith("那") or continuation)
        and not _SWITCH.search(system_text)
    )
    if _STANDALONE.search(current) and not _PERSONAL.search(current):
        followup = False
    prior_project = project_of(prior)
    if project and prior_project and project != prior_project:
        followup = False
    previous = prior if followup and prior != current else ""
    project = project or (prior_project if previous else "")
    if project:
        kind, reason = "project", "explicit_project_or_anchored_followup"
    elif _PERSONAL.search(current) or (previous and _PERSONAL.search(previous)):
        kind, reason = "personal", "personal_facts_need_evidence"
    elif _GENERAL.search(current) or _STANDALONE.search(current) or previous:
        kind, reason = "general", "general_question_without_project_anchor"
    else:
        kind, reason = "uncertain", "keep_retrieval_for_ambiguous_question"
    query = _CODA.sub("CODA PaiCLI", current)
    if project == "coda":
        if re.search(r"单\s*Agent|多\s*Agent|单智能体|多智能体", current, re.I):
            query += " ReAct Plan Team 模式选择"
        if re.search(r"长短期记忆|长期记忆|记忆怎么|Memory|save_memory", current, re.I):
            query += " Memory Context ManagedMemoryStore save_memory"
        if re.search(r"多工具|工具调用|写同一个文件|互斥|版本检查|expected_sha|CommandGuard", current, re.I):
            query += " ToolRegistry ResourceAccess Wave expected_sha256 CommandGuard"
        if re.search(r"失败重试|重试机制|DAG.{0,20}失败|熔断|恢复", current, re.I):
            query += " RetryingLlmClient max_attempts base_delay_seconds Retry-After AgentBudget stagnation_window Plan replan Team review retry"
        if re.search(r"最大.{0,8}难点|工程难点|多文件|改到一半", current, re.I):
            query += " Plan ToolRegistry multi_edit expected_sha256 atomic write"
    if project == "enterprise_rag":
        if re.search(r"切块|chunk|重叠|overlap", current, re.I):
            query += " 1200 200 chunk overlap"
        if re.search(r"几路|四路|检索|RRF|retriever", current, re.I):
            query += " four-route weighted RRF retriever_k rrf_k 2048"
        if re.search(r"98\.09|Hit@10|All-Gold|MRR", current, re.I):
            query += " Hit@10 98.09 All-Gold 92.34 MRR 470"
        if re.search(r"Matryoshka|套娃|MRL|维度", current, re.I):
            query += " Matryoshka MRL embedding dimension Qwen3-Embedding-4B"
    if previous and project in ("coda", "enterprise_rag"):
        # Carry identity, not the whole previous topic. A broad previous Plan
        # question otherwise overwhelms a narrow recovery/failure follow-up.
        anchor = "CODA PaiCLI" if project == "coda" else "EnterpriseRAG"
        if project == "coda" and re.search(r"恢复|续跑|重跑|中断", current):
            anchor += " Resume Checkpoint"
        query = query + "\n项目与主题：" + anchor
    elif previous:
        query = "上一题：" + _CODA.sub("CODA PaiCLI", previous) + "\n当前追问：" + query
    return QuestionRoute(kind, project, current, query, previous, microphone, reason)


def generation_prompt(base: str, route: QuestionRoute) -> str:
    expanded = re.search(r"怎么|如何|为什么|代价|过程|取舍|具体|分别", route.current)
    fact = re.search(r"多少|几个|几种|是不是|是[^？?]{0,80}吗|是什么指标|准确率吗", route.current)
    subquestions = max(1, len(re.findall(r"[？?]", route.current)))
    if fact and not expanded and subquestions == 1:
        constraint = (
            "本题是单一事实确认。最终答案硬上限180个汉字，最多两个短自然段。"
            "只回答被问到的事实和一个必要解释；达到上限时先删除例子、旁支指标和重复说明。"
            "证据不足只说具体缺口，不编造数量，不把一般验证方法展开成答案。"
        )
    elif subquestions >= 4:
        constraint = (
            "本题包含多个子问题。最终答案硬上限520个汉字，最多四个短自然段，总共不超过8句；每个明确子问题用1到2句回答。"
            "达到上限时先删除例子、旁支模块、审计过程和重复结论，不删关键条件与失败边界。"
            "不要另加总结段。"
        )
    elif subquestions >= 2:
        constraint = (
            "本题包含多个子问题。最终答案硬上限460个汉字，最多三个短自然段，总共不超过6句；每个子问题用1到2句回答，不要列清单。"
            "达到上限时先删除例子、旁支模块、审计过程和重复结论，不删关键条件与失败边界。"
            "不要另加总结段。"
        )
    else:
        constraint = (
            "本题需要解释。最终答案硬上限360个汉字，最多三个短自然段，总共不超过5句。"
            "直接讲当前问题需要的机制和取舍；达到上限时先删除例子、旁支和重复结论。"
            "不要另加总结段。"
        )
    return base + "\n\n本轮输出约束：\n" + constraint


@lru_cache(maxsize=1)
def coda_source_names() -> frozenset[str]:
    try:
        names = json.loads(Path(__file__).with_name("coda_source_names.json").read_text(encoding="utf-8"))
        return frozenset(x for x in names if isinstance(x, str)) if isinstance(names, list) else frozenset()
    except (OSError, ValueError):
        return frozenset()



@dataclass(frozen=True)
class RetrievalGroup:
    name: str
    terms: tuple[str, ...]
    strong_terms: tuple[str, ...] = ()


def retrieval_groups(route: QuestionRoute) -> tuple[RetrievalGroup, ...]:
    """Build deterministic evidence groups; no extra model call is required."""
    current = route.current
    groups: list[RetrievalGroup] = []

    def add(
        name: str,
        terms: tuple[str, ...],
        pattern: str,
        strong_terms: tuple[str, ...] = (),
    ) -> None:
        if re.search(pattern, current, re.I) and not any(group.name == name for group in groups):
            groups.append(RetrievalGroup(name, terms, strong_terms))

    if route.project == "coda":
        add(
            "memory_runtime",
            ("MemoryManager", "ManagedMemoryStore", "短期记忆", "长期记忆", "Context", "SQLite"),
            r"长短期记忆|长期记忆|短期记忆|Memory|save_memory",
            ("MemoryManager", "ManagedMemoryStore"),
        )
        add(
            "memory_trust",
            ("save_memory", "UNVERIFIED", "VERIFIED", "来源", "信任", "检索"),
            r"save_memory|错误记忆|污染|信任|验证",
            ("save_memory", "UNVERIFIED", "VERIFIED"),
        )
        add(
            "tool_waves",
            ("execute_many_results", "Wave", "ResourceAccess", "读写冲突", "写写冲突", "资源声明"),
            r"多个工具|多工具|工具调用|并发|互斥|写同一个文件",
            ("execute_many_results", "Wave", "ResourceAccess"),
        )
        add(
            "file_version",
            ("expected_sha256", "TOCTOU", "atomic", "os.replace", "版本检查", "单文件"),
            r"版本检查|expected_sha|多文件|改到一半|写同一个文件",
            ("expected_sha256", "TOCTOU", "os.replace"),
        )
        add(
            "command_guard",
            ("CommandGuard", "应用层", "shell", "cwd", "chroot", "sandbox", "黑名单"),
            r"命令黑名单|CommandGuard|shell|沙箱",
            ("CommandGuard", "chroot", "sandbox"),
        )
        add(
            "transport_retry",
            ("RetryingLlmClient", "max_attempts", "base_delay_seconds", "Retry-After", "0.25", "0.5", "jitter"),
            r"网络抖动|重试几次|失败重试|重试机制|Retry",
            ("RetryingLlmClient", "max_attempts", "base_delay_seconds", "0.25", "0.5"),
        )
        add(
            "stagnation",
            ("AgentBudget", "stagnation_window", "3", "重复", "停滞", "warning"),
            r"停止|停滞|重复|重试几次|失败重试",
            ("AgentBudget", "stagnation_window"),
        )
        add(
            "plan_failure",
            ("PlanExecuteAgent", "replan", "completed_results", "DAG", "替代计划", "依赖"),
            r"DAG|子任务失败|Replan|恢复|失败以后",
            ("PlanExecuteAgent", "replan", "completed_results"),
        )
        add(
            "team_retry",
            ("Reviewer", "review_retries", "retryable", "Worker", "返工"),
            r"失败重试|重试机制|Reviewer|Team",
            ("Reviewer", "review_retries", "retryable"),
        )
        add(
            "planning",
            ("Planner", "DAG", "Plan", "ReAct", "依赖", "环检测", "调度"),
            r"工程难点|最大.{0,8}难点|规划|Plan",
            ("Planner", "DAG", "ReAct"),
        )
        add(
            "multi_edit",
            ("multi_edit", "prepare_text_edits", "FileMutation", "write_mutations", "os.replace", "partial"),
            r"多文件|改到一半|multi_edit|原子",
            ("multi_edit", "prepare_text_edits", "FileMutation", "write_mutations"),
        )
    elif route.project == "enterprise_rag":
        add(
            "workflow",
            ("Document", "Evidence", "Prompt", "Answer", "EvidenceBuilder", "Query-Spans Packing"),
            r"端到端|流程|主线",
            ("EvidenceBuilder", "Query-Spans Packing"),
        )
        add(
            "chunking",
            ("1200", "200", "chunk", "overlap", "10722", "115406"),
            r"切块|chunk|重叠|overlap",
            ("1200", "200", "overlap", "115406"),
        )
        add(
            "retrieval",
            ("Dense", "BM25", "Keyword BM25", "English BM25", "Weighted RRF", "rrf_k", "retriever_k"),
            r"几路|四路|检索|RRF|retriever|BM25",
            ("Weighted RRF", "rrf_k", "retriever_k", "Keyword BM25", "English BM25"),
        )
        add(
            "embedding",
            ("Qwen3-Embedding-4B", "E5", "2048", "Dense", "95.96", "97.87"),
            r"Qwen3|Embedding|E5|为什么用",
            ("Qwen3-Embedding-4B", "2048", "95.96", "97.87"),
        )
        add(
            "metrics",
            ("Hit@10", "98.09", "All-Gold@10", "92.34", "MRR", "470"),
            r"98\.09|Hit@10|All-Gold|MRR|指标|准确率",
            ("98.09", "All-Gold@10", "92.34", "MRR"),
        )
        add(
            "rerank",
            ("rerank", "MiniLM", "BGE", "回退", "94.47", "强检索"),
            r"rerank|重排|CrossEncoder",
            ("MiniLM", "BGE", "94.47", "回退"),
        )
        add(
            "matryoshka",
            ("Matryoshka", "MRL", "维度", "截断", "embedding"),
            r"Matryoshka|套娃|MRL|维度",
            ("Matryoshka", "MRL"),
        )

    if not groups:
        pieces = [
            piece.strip(" ，,:：。.!！")
            for piece in re.split(r"[？?；;]", current)
            if len(piece.strip()) >= 4
        ]
        for index, piece in enumerate(pieces[:6], start=1):
            terms = tuple(
                token for token in re.findall(r"[A-Za-z][A-Za-z0-9_.@-]{1,}|[0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]{2,8}", piece)
                if token not in {"什么", "怎么", "为什么", "如何", "是不是", "这个", "那个"}
            )
            if terms:
                groups.append(RetrievalGroup(f"question_{index}", terms[:8]))
    return tuple(groups)


def broad_retrieval_query(route: QuestionRoute) -> str:
    """One provider query that explicitly asks retrieval to cover independent facts."""
    groups = retrieval_groups(route)
    if len(groups) <= 1:
        return route.query
    anchor = ""
    if route.project == "coda":
        anchor = "项目：CODA PaiCLI\n"
    elif route.project == "enterprise_rag":
        anchor = "项目：EnterpriseRAG\n"
    lines = [
        f"- {group.name}: " + " ".join(group.terms)
        for group in groups
    ]
    return (
        f"{anchor}"
        "这是多子问题检索。优先返回具体参数卡、机制卡、实验卡和边界说明，"
        "避免多个重复的项目总览占满候选。\n"
        "请同时检索下列相互独立的事实主题，每个主题都需要直接证据：\n"
        + "\n".join(lines)
        + f"\n原问题仅用于限定语境，不要按整题只返回一篇总览：{route.current}"
    )


_COVERAGE_METADATA_KEYS = frozenset({
    "id",
    "parent_id",
    "project",
    "round",
    "module",
    "kind",
    "category",
    "content_type",
    "topic",
    "keywords",
    "knowledge_scope",
    "status",
    "answer_type",
    "difficulty",
    "priority",
    "question",
    "code_revision",
    "review_date",
})


def _coverage_evidence_text(hit: KnowledgeHit) -> str:
    """Return only evidence prose for coverage scoring.

    Bailian chunks may repeat document metadata inside hit.text. Those fields
    are useful for retrieval, but they are not proof that the fact is present
    in the chunk body. Coverage therefore ignores document name/title and a
    leading metadata block such as keywords.
    """
    text = hit.text
    if "【正文】:" in text:
        text = text.split("【正文】:", 1)[1]

    body: list[str] = []
    metadata_prefix = True
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if metadata_prefix and not line:
            continue
        key = ""
        if ":" in line:
            key = line.split(":", 1)[0].strip().lower()
        if metadata_prefix and key in _COVERAGE_METADATA_KEYS:
            continue
        metadata_prefix = False
        if line:
            body.append(line)

    return re.sub(r"\s+", " ", " ".join(body)).lower()


def _coverage_score(hit: KnowledgeHit, group: RetrievalGroup) -> tuple[float, tuple[str, ...]]:
    haystack = _coverage_evidence_text(hit)
    matched: list[str] = []
    total_weight = 0.0
    matched_weight = 0.0
    for term in group.terms:
        normalized = term.lower()
        weight = 2.0 if re.search(r"[0-9_@.-]", normalized) or re.search(r"[A-Za-z]", normalized) else 1.0
        total_weight += weight
        if normalized in haystack:
            matched.append(term)
            matched_weight += weight
    if not matched or total_weight <= 0:
        return 0.0, ()
    strong = {term.lower() for term in group.strong_terms}
    strong_match = any(term.lower() in strong for term in matched)
    if group.strong_terms and not strong_match and len(matched) < 2:
        return 0.0, ()
    coverage = matched_weight / total_weight
    # A single identifier from an overview paragraph is not enough evidence for
    # a multi-fact group. This also prevents a metadata-adjacent generic mention
    # from occupying the group while a more specific chunk is available.
    if coverage < 0.25:
        return 0.0, ()
    return coverage, tuple(matched)


def _fit_evidence_budget(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
    fitted: list[KnowledgeHit] = []
    used = 0
    for hit in hits:
        remaining = MAX_FINAL_EVIDENCE_CHARS - used
        if remaining <= 0:
            break
        text = hit.text
        if len(text) > remaining:
            if remaining < 300:
                break
            text = text[:remaining].rstrip()
            hit = replace(hit, text=text)
        fitted.append(hit)
        used += len(text)
    return fitted


def filter_knowledge(result: KnowledgeSearchResult, route: QuestionRoute) -> tuple[KnowledgeSearchResult, dict]:
    """Filter candidates, then prefer evidence coverage over global rank only.

    Exact duplicates and identifiable wrong-project hits are removed first. For
    multi-part questions, one direct piece of evidence is selected per retrieval
    group when possible, then remaining slots are filled by provider rank. Numeric
    measurements and limitations are never rewritten; similarity is not factual
    confidence.
    """
    hits = []
    seen: set[str] = set()
    rejected: list[dict] = []
    for hit in result.hits:
        metadata = hit.document_name + " " + hit.title
        frontmatter = re.match(r"\s*---\s*\n(.*?)\n---", hit.text, re.S)
        if frontmatter:
            metadata += " " + frontmatter.group(1)
        source_project = project_of(metadata)
        source_name = hit.document_name.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md").strip()
        if not source_project and source_name in coda_source_names():
            source_project = "coda"
        normalized = re.sub(r"\s+", " ", hit.text).strip()
        reason = ""
        if normalized in seen:
            reason = "exact_duplicate"
        elif route.project in ("coda", "enterprise_rag") and source_project in ("coda", "enterprise_rag") and route.project != source_project:
            reason = "different_project"
        if reason:
            rejected.append({"source": hit.document_name or hit.title, "reason": reason})
            continue
        seen.add(normalized)
        hits.append(hit)
    groups = retrieval_groups(route)
    assignments: dict[str, dict] = {}
    selected_indices: list[int] = []

    for group in groups:
        ranked: list[tuple[float, float, int, tuple[str, ...]]] = []
        for index, hit in enumerate(hits):
            coverage, matched = _coverage_score(hit, group)
            if coverage <= 0:
                continue
            provider_score = hit.score if isinstance(hit.score, (int, float)) else 0.0
            ranked.append((coverage, provider_score, -index, matched))
        if not ranked:
            assignments[group.name] = {"covered": False}
            continue
        coverage, provider_score, neg_index, matched = max(ranked)
        index = -neg_index
        if index not in selected_indices:
            selected_indices.append(index)
        assignments[group.name] = {
            "covered": True,
            "source": hits[index].document_name or hits[index].title,
            "coverage_score": round(coverage, 4),
            "provider_score": provider_score,
            "matched_terms": list(matched),
        }

    target_hits = 3 if len(groups) <= 1 else min(
        MAX_FINAL_EVIDENCE_HITS,
        max(5, len(groups) + 1),
    )
    for index in range(len(hits)):
        if len(selected_indices) >= target_hits:
            break
        if index not in selected_indices:
            selected_indices.append(index)

    selected = _fit_evidence_budget([hits[index] for index in selected_indices])
    status = "hit" if selected else ("empty" if result.status == "hit" else result.status)
    filtered = replace(result, status=status, hits=tuple(selected))
    covered_groups = sum(1 for value in assignments.values() if value.get("covered"))
    return filtered, {
        "received_hits": len(result.hits),
        "candidate_hits_after_filter": len(hits),
        "accepted_hits": len(selected),
        "retrieval_groups": [group.name for group in groups],
        "covered_groups": covered_groups,
        "missing_groups": [name for name, value in assignments.items() if not value.get("covered")],
        "group_assignments": assignments,
        "rejected": rejected,
    }
