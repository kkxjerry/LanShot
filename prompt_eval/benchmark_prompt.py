#!/usr/bin/env python3
"""Replay the 12 interview questions through LanShot's real routing/RAG path.

No result files are written by default. The script prints compact JSON so prompt
variants can be compared without changing the production prompt.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lanshot2.audio_service import (
    GeminiQuestionClient,
    VoiceQuestionClient,
    load_api_key,
    load_google_api_key,
)
from lanshot_common.interview_rag import (
    broad_retrieval_query,
    filter_knowledge,
    generation_prompt,
    route_question,
)
from lanshot_common.knowledge import (
    KnowledgeSearchResult,
    KnowledgeService,
    ground_text,
)

DATASET = Path(__file__).with_name("interview_12_cases.json")
BASELINE_PROMPT = ROOT / "lanshot2" / "voice_question_prompt.txt"
CANDIDATE_PROMPT = Path(__file__).with_name("voice_question_prompt_deepseek_v4.txt")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_deepseek_key() -> str:
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    if sys.platform == "darwin":
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "com.lanshot.deepseek",
                "-a",
                "DEEPSEEK_API_KEY",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise RuntimeError("未找到 DEEPSEEK_API_KEY")


class DeepSeekOfficialClient:
    """Streaming client for prompt evaluation; reasoning is never shown."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-flash",
        reasoning_effort: str = "low",
        thinking: bool = True,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.thinking = thinking
        self.last_timing: dict[str, Any] = {}
        self.last_usage: dict[str, Any] = {}

    def ask_stream(
        self,
        question: str,
        prompt: str,
        history: list[dict] | None = None,
        *,
        on_update=None,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        for item in history or []:
            messages.extend(
                [
                    {"role": "user", "content": item["input"]},
                    {"role": "assistant", "content": item["answer"]},
                ]
            )
        messages.append({"role": "user", "content": question})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 2400,
        }
        payload["reasoning_effort"] = self.reasoning_effort if self.thinking else "none"
        request = urllib.request.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        started = time.monotonic()
        first_visible_ms: int | None = None
        parts: list[str] = []
        finish_reason = ""
        event_count = 0
        received = 0
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                for raw in response:
                    received += len(raw)
                    if received > 4 * 1024 * 1024:
                        raise RuntimeError("DeepSeek 返回内容过大")
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    encoded = line[5:].strip()
                    if not encoded or encoded == "[DONE]":
                        continue
                    event = json.loads(encoded)
                    if isinstance(event.get("usage"), dict):
                        self.last_usage = event["usage"]
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    event_count += 1
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        if first_visible_ms is None:
                            first_visible_ms = round((time.monotonic() - started) * 1000)
                        parts.append(content)
                        if on_update is not None:
                            on_update("".join(parts).strip())
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"DeepSeek 请求失败，HTTP {status}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise RuntimeError("DeepSeek 流式请求失败") from error
        answer = "".join(parts).strip()
        self.last_timing = {
            "provider": "deepseek_official",
            "model": self.model,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort if self.thinking else "none",
            "first_visible_ms": first_visible_ms,
            "complete_ms": round((time.monotonic() - started) * 1000),
            "finish_reason": finish_reason,
            "event_count": event_count,
        }
        if finish_reason == "length":
            raise RuntimeError("DeepSeek 答案达到输出上限")
        if not answer:
            raise RuntimeError("DeepSeek 没有返回可见答案")
        return answer


def build_answer_client(provider: str, model: str, effort: str, thinking: bool):
    if provider == "bailian":
        return VoiceQuestionClient(
            load_api_key(),
            model=model,
            enable_thinking=thinking,
            reasoning_effort=effort,
        )
    if provider == "deepseek":
        return DeepSeekOfficialClient(
            load_deepseek_key(),
            model=model,
            reasoning_effort=effort,
            thinking=thinking,
        )
    raise ValueError(provider)


def search_knowledge(
    service: KnowledgeService,
    question: str,
    expected_project: str = "",
    previous_question: str = "",
) -> tuple[Any, KnowledgeSearchResult, dict[str, Any]]:
    route = route_question(question, "", previous_question=previous_question)
    if expected_project and route.project != expected_project:
        anchor = "CODA PaiCLI" if expected_project == "coda" else "EnterpriseRAG"
        route = replace(
            route,
            kind="project",
            project=expected_project,
            query=route.current + "\n项目与主题：" + anchor,
            reason="benchmark_project_hint",
        )
    if not route.use_knowledge:
        return route, KnowledgeSearchResult.disabled(route.query), {
            "received_hits": 0,
            "accepted_hits": 0,
            "rejected": [],
        }
    search_query = broad_retrieval_query(route)
    candidate_search = getattr(service, "search_candidates", None)
    raw = candidate_search(search_query) if callable(candidate_search) else service.search(search_query)
    filtered, audit = filter_knowledge(raw, route)
    return route, filtered, audit


def answer_case(client, service: KnowledgeService, case: dict[str, Any], prompt: str) -> dict[str, Any]:
    route, knowledge, audit = search_knowledge(
        service,
        case["question"],
        case.get("project", ""),
        case.get("previous_question", ""),
    )
    grounded = ground_text(route.generation_input(), knowledge, coverage=audit)
    effective_prompt = generation_prompt(prompt, route, coverage=audit)
    started = time.monotonic()
    answer = client.ask_stream(grounded, effective_prompt, history=[])
    timing = dict(getattr(client, "last_timing", {}))
    timing["end_to_end_ms"] = round((time.monotonic() - started) * 1000)
    return {
        "answer": answer,
        "chars": len(answer),
        "route": route.as_dict(),
        "knowledge": knowledge.summary(),
        "knowledge_audit": audit,
        "timing": timing,
        "markdown_flags": markdown_flags(answer),
    }


def markdown_flags(answer: str) -> list[str]:
    flags: list[str] = []
    fence = chr(96) * 3
    if fence in answer:
        flags.append("code_fence")
    if re.search(r"(?m)^\s{0,3}#{1,6}\s", answer):
        flags.append("heading")
    if re.search(r"(?m)^\s*[-*+]\s+", answer):
        flags.append("bullet")
    if re.search(r"(?m)^\s*\d+[.)]\s+", answer):
        flags.append("numbered_list")
    if re.search(r"(?m)^\s*>\s+", answer):
        flags.append("quote")
    return flags


JUDGE_SYSTEM = """你是技术面试答案评测器。你只评价答案，不修改答案。
依据题目的 must_cover 和 must_not_claim 检查两份候选回答。

评分：
correctness 0-50：技术事实准确，must_cover 中的重要内容是否回答。
boundary 0-20：有没有把支持写成强制、设计写成已实现、局部能力写成整体保证；命中 must_not_claim 要明显扣分。
coverage 0-15：是否覆盖当前题目的多个子问题，而不是只答其中一角。
oral 0-10：是否直接、自然、易口述，不是堆术语和模板话。
discipline 0-5：不编个人经历和无来源数字，不混淆指标口径。

total 必须等于五项之和。不要因为某份答案更长就给高分。
只输出 JSON，不要 Markdown，不要额外解释。JSON 格式：
{"a":{"correctness":0,"boundary":0,"coverage":0,"oral":0,"discipline":0,"total":0,"errors":["..."]},
 "b":{"correctness":0,"boundary":0,"coverage":0,"oral":0,"discipline":0,"total":0,"errors":["..."]},
 "winner":"a|b|tie","reason":"一句话"}"""


def parse_json_object(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = cleaned[len(fence):].lstrip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    if cleaned.endswith(fence):
        cleaned = cleaned[:-len(fence)].rstrip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("judge did not return JSON")
    return json.loads(cleaned[start : end + 1])


def judge_pair(judge, case: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    user = json.dumps(
        {
            "question": case["question"],
            "must_cover": case["must_cover"],
            "must_not_claim": case["must_not_claim"],
            "answer_a": a,
            "answer_b": b,
        },
        ensure_ascii=False,
    )
    raw = judge.ask(user, JUDGE_SYSTEM, history=[])
    return parse_json_object(raw)


def compact_result(
    case: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    judge: dict[str, Any] | None,
) -> dict[str, Any]:
    def view(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "chars": value["chars"],
            "preview": value["answer"][:260],
            "markdown_flags": value["markdown_flags"],
            "knowledge": value["knowledge"],
            "timing": value["timing"],
        }

    return {
        "id": case["id"],
        "title": case["title"],
        "baseline": view(baseline),
        "candidate": view(candidate),
        "judge": judge,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--baseline-prompt", type=Path, default=BASELINE_PROMPT)
    parser.add_argument("--candidate-prompt", type=Path, default=CANDIDATE_PROMPT)
    parser.add_argument("--provider", choices=("bailian", "deepseek"), default="bailian")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--effort", choices=("low", "high", "max"), default="low")
    parser.add_argument("--thinking", choices=("on", "off"), default="on")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--variant", choices=("baseline", "candidate", "both"), default="both")
    parser.add_argument("--route-only", action="store_true")
    parser.add_argument("--retrieval-only", action="store_true")
    args = parser.parse_args()

    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    selected = {x.strip() for x in args.ids.split(",") if x.strip()}
    if selected:
        cases = [case for case in cases if case["id"] in selected]
    if args.limit > 0:
        cases = cases[: args.limit]

    if args.route_only:
        mismatches = 0
        for case in cases:
            route = route_question(
                case["question"], "", previous_question=case.get("previous_question", "")
            )
            expected_project = case.get("project", "")
            project_ok = route.project == expected_project
            if not project_ok:
                mismatches += 1
            print(json.dumps({
                "id": case["id"],
                "expected_project": expected_project,
                "kind": route.kind,
                "project": route.project,
                "use_knowledge": route.use_knowledge,
                "project_ok": project_ok,
                "query": route.query,
            }, ensure_ascii=False), flush=True)
        print(json.dumps({"type":"route_summary","case_count":len(cases),"project_mismatches":mismatches}, ensure_ascii=False))
        return 0

    if args.retrieval_only:
        service = KnowledgeService(load_api_key)
        for case in cases:
            route, knowledge, audit = search_knowledge(
                service,
                case["question"],
                case.get("project", ""),
                case.get("previous_question", ""),
            )
            print(json.dumps({
                "id": case["id"],
                "route": route.as_dict(),
                "knowledge": knowledge.summary(),
                "audit": audit,
                "hits": [
                    {
                        "score": hit.score,
                        "document_name": hit.document_name,
                        "title": hit.title,
                        "preview": hit.text[:500],
                    }
                    for hit in knowledge.hits
                ],
            }, ensure_ascii=False), flush=True)
        return 0

    baseline_prompt = read_text(args.baseline_prompt)
    candidate_prompt = read_text(args.candidate_prompt)
    client = build_answer_client(
        args.provider,
        args.model,
        args.effort,
        args.thinking == "on",
    )
    knowledge = KnowledgeService(load_api_key)
    judge = None
    if not args.skip_judge:
        judge = GeminiQuestionClient(
            load_google_api_key(),
            thinking_level="LOW",
        )

    results = []
    baseline_scores: list[float] = []
    candidate_scores: list[float] = []
    winners = {"a": 0, "b": 0, "tie": 0}

    for case in cases:
        baseline = answer_case(client, knowledge, case, baseline_prompt) if args.variant in ("baseline", "both") else None
        candidate = answer_case(client, knowledge, case, candidate_prompt) if args.variant in ("candidate", "both") else None
        if baseline is not None and candidate is not None:
            judged = judge_pair(judge, case, baseline["answer"], candidate["answer"]) if judge else None
            if judged:
                baseline_scores.append(float(judged["a"]["total"]))
                candidate_scores.append(float(judged["b"]["total"]))
                key = judged.get("winner", "tie")
                winners[key] = winners.get(key, 0) + 1
            compact = compact_result(case, baseline, candidate, judged)
        else:
            value = baseline if baseline is not None else candidate
            compact = {
                "id": case["id"],
                "title": case["title"],
                "variant": args.variant,
                "answer": value["answer"],
                "chars": value["chars"],
                "markdown_flags": value["markdown_flags"],
                "knowledge": value["knowledge"],
                "knowledge_audit": value["knowledge_audit"],
                "route": value["route"],
                "timing": value["timing"],
            }
        results.append(compact)
        print(json.dumps(compact, ensure_ascii=False), flush=True)

    summary = {
        "type": "summary",
        "provider": args.provider,
        "model": args.model,
        "case_count": len(results),
        "baseline_prompt_chars": len(baseline_prompt),
        "candidate_prompt_chars": len(candidate_prompt),
        "baseline_avg": round(sum(baseline_scores) / len(baseline_scores), 2) if baseline_scores else None,
        "candidate_avg": round(sum(candidate_scores) / len(candidate_scores), 2) if candidate_scores else None,
        "winners": winners if baseline_scores else None,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
