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

from lanshot_common.knowledge import KnowledgeSearchResult

_QUESTION = re.compile(r"第\s*(?:[0-9]+|[一二三四五六七八九十百]+)\s*题\s*[，,:：。.!！?？]*\s*")
_CODA = re.compile(r"(?<![A-Za-z0-9])(?:codah?|paicli)(?![A-Za-z0-9])", re.I)
_ENTERPRISE = re.compile(r"Enterprise\s*RAG|minirag|企业(?:级)?\s*RAG", re.I)
_PERSONAL = re.compile(r"你的|你们的|你做的|你开发的|你之前|你当时|我的|我们(?:的|在)|个人贡献|负责|实习|简历|项目")
_FOLLOWUP = re.compile(r"^(?:那[，, ]*)?(?:这个|这种|这些|它|刚才|上面|前面|你说的|你提到的|你接入的|为什么是\s*[0-9一二三四五六七八九十百]|那么|那为什么|那怎么)|(?:刚才|你说的|你提到的)", re.I)
_SWITCH = re.compile(r"换(?:个|一个|一道)问题|下(?:个|一个|一道)问题|接下来(?:问|聊)|另外问")
_STANDALONE = re.compile(r"HNSW|MySQL|Redis|GIL|TCP|HTTP|Linux|JVM|垃圾回收|进程和线程|进程与线程", re.I)
_GENERAL = re.compile(r"什么是|解释|区别|原理|适合|如何|怎么|算法|比较|SQL|为什么|有哪些", re.I)
_FOCUS = re.compile(r"^(?:嗯|啊|哦|好的|好|对|是的|谢谢|继续)[。.!！?？,， ]*$")
MAX_TURN_CHARS = 1800
MAX_PREVIOUS_CHARS = 900
MAX_MICROPHONE_CHARS = 1000


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
    prior = interviewer_from_archive(previous_question) if previous_question else ""
    followup = bool(prior and (_FOLLOWUP.search(current) or current.startswith("那")) and not _SWITCH.search(system_text))
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
        if re.search(r"长期记忆|记忆怎么|Memory", current, re.I):
            query += " Memory Context"
    if previous and project in ("coda", "enterprise_rag"):
        # Carry identity, not the whole previous topic. A broad previous Plan
        # question otherwise overwhelms a narrow recovery/failure follow-up.
        anchor = "CODA PaiCLI" if project == "coda" else "EnterpriseRAG"
        if project == "coda" and re.search(r"恢复|续跑|重跑|中断", current):
            anchor += " Resume Checkpoint"
        query = current + "\n项目与主题：" + anchor
    elif previous:
        query = "上一题：" + _CODA.sub("CODA PaiCLI", previous) + "\n当前追问：" + query
    return QuestionRoute(kind, project, current, query, previous, microphone, reason)


def generation_prompt(base: str, route: QuestionRoute) -> str:
    expanded = re.search(r"怎么|如何|为什么|代价|过程|取舍|具体|分别", route.current)
    fact = re.search(r"多少|几个|几种|是不是|是[^？?]{0,80}吗|是什么指标|准确率吗", route.current)
    if fact and not expanded:
        constraint = (
            "本题是单一事实确认。用一到两个短自然段，尽量在80至180个汉字内说清。"
            "只回答被问到的事实和一个必要解释；不要追加其他指标、实验分母或长篇建议。"
            "证据不足只说具体缺口，不编造数量，不把一般验证方法展开成答案。"
        )
    else:
        constraint = (
            "本题需要解释。优先用约280至480个汉字、最多三个自然段，覆盖所有子问题。"
            "同一原因只讲一次，不再追加总结段。只展开当前问题所需的机制和取舍，"
            "不主动列出旁支代码差异或审计过程。多Agent或独立审查不能保证正确，也可能共享错误。"
        )
    return base + "\n\n本轮输出约束：\n" + constraint


@lru_cache(maxsize=1)
def coda_source_names() -> frozenset[str]:
    try:
        names = json.loads(Path(__file__).with_name("coda_source_names.json").read_text(encoding="utf-8"))
        return frozenset(x for x in names if isinstance(x, str)) if isinstance(names, list) else frozenset()
    except (OSError, ValueError):
        return frozenset()


def filter_knowledge(result: KnowledgeSearchResult, route: QuestionRoute) -> tuple[KnowledgeSearchResult, dict]:
    """Drop exact duplicates and identifiable wrong-project hits, not low scores.

    Unknown source identity is retained. Numeric measurements and limitations are
    never rewritten. This does not claim that a high similarity is factual proof.
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
    status = "hit" if hits else ("empty" if result.status == "hit" else result.status)
    filtered = replace(result, status=status, hits=tuple(hits))
    return filtered, {"received_hits": len(result.hits), "accepted_hits": len(hits), "rejected": rejected}
