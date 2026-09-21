#!/usr/bin/env python3
"""Compare Bailian DeepSeek models on the same LanShot prompt/RAG evidence."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import benchmark_prompt as bench  # noqa: E402
from lanshot2.audio_service import (  # noqa: E402
    BAILIAN_URL,
    GeminiQuestionClient,
    VoiceQuestionClient,
    load_api_key,
    load_google_api_key,
)
from lanshot_common.interview_rag import generation_prompt  # noqa: E402
from lanshot_common.knowledge import KnowledgeService, ground_text  # noqa: E402


DEFAULT_MODELS = (
    "deepseek-v4.1-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
)
DEFAULT_IDS = ("01", "02", "07", "09", "10", "12")
DEFAULT_PROMPT = Path(__file__).with_name("voice_question_prompt_deepseek_v4.txt")


MATRIX_JUDGE_SYSTEM = """你是技术面试答案评测器。对同一道题的多个模型回答逐个独立打分，不做相对排名，不因为答案更长给高分。

依据 must_cover 和 must_not_claim 评分：
correctness 0-50：技术事实正确且覆盖关键事实。
boundary 0-20：不把可选说成强制，不把通用设计说成项目已实现，不扩大结论范围。
coverage 0-15：覆盖题目明确询问的子问题。
oral 0-10：直接、自然、可口述，不堆术语。
discipline 0-5：不编个人经历/数字，不混淆指标口径。
total 必须等于五项之和。

只输出 JSON 对象：
{"scores":{"模型名":{"correctness":0,"boundary":0,"coverage":0,"oral":0,"discipline":0,"total":0,"errors":["..."]}}}
不要 Markdown，不要额外文字。"""


def discover_deepseek_models() -> list[str]:
    root = BAILIAN_URL.rsplit("/chat/completions", 1)[0]
    request = urllib.request.Request(
        root + "/models",
        headers={"Authorization": "Bearer " + load_api_key()},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.loads(response.read())
    return sorted(
        item.get("id", "")
        for item in value.get("data", [])
        if isinstance(item, dict) and "deepseek" in item.get("id", "").lower()
    )


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return round(float(ordered[index]), 1)


def prepare_cases(
    cases: list[dict[str, Any]],
    prompt: str,
) -> list[dict[str, Any]]:
    service = KnowledgeService(load_api_key)
    prepared = []
    for case in cases:
        route, knowledge, audit = bench.search_knowledge(
            service,
            case["question"],
            case.get("project", ""),
            case.get("previous_question", ""),
        )
        prepared.append(
            {
                "case": case,
                "route": route,
                "knowledge": knowledge,
                "knowledge_audit": audit,
                "question": ground_text(route.generation_input(), knowledge, coverage=audit),
                "prompt": generation_prompt(prompt, route, coverage=audit),
            }
        )
    return prepared


def run_one(
    model: str,
    item: dict[str, Any],
    *,
    thinking: bool,
    effort: str,
) -> dict[str, Any]:
    client = VoiceQuestionClient(
        load_api_key(),
        model=model,
        enable_thinking=thinking,
        reasoning_effort=effort,
    )
    started = time.monotonic()
    try:
        answer = client.ask_stream(
            item["question"],
            item["prompt"],
            history=[],
        )
        timing = dict(client.last_timing)
        timing["wall_ms"] = round((time.monotonic() - started) * 1000)
        return {
            "status": "ok",
            "answer": answer,
            "chars": len(answer),
            "markdown_flags": bench.markdown_flags(answer),
            "timing": timing,
            "usage": dict(getattr(client, "last_usage", {})),
        }
    except Exception as error:
        return {
            "status": "error",
            "error": f"{type(error).__name__}: {error}",
            "chars": 0,
            "markdown_flags": [],
            "timing": {"wall_ms": round((time.monotonic() - started) * 1000)},
            "usage": {},
        }


def judge_case(
    judge: GeminiQuestionClient,
    case: dict[str, Any],
    answers: dict[str, str],
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "question": case["question"],
            "must_cover": case["must_cover"],
            "must_not_claim": case["must_not_claim"],
            "answers": answers,
        },
        ensure_ascii=False,
    )
    raw = judge.ask(payload, MATRIX_JUDGE_SYSTEM, history=[])
    return bench.parse_json_object(raw).get("scores", {})


def summarize(
    models: list[str],
    case_ids: list[str],
    rows: list[dict[str, Any]],
    judge_scores: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for model in models:
        current = [row for row in rows if row["model"] == model]
        ok = [row for row in current if row["result"]["status"] == "ok"]
        ttft = [
            float(row["result"]["timing"]["first_visible_ms"])
            for row in ok
            if isinstance(row["result"]["timing"].get("first_visible_ms"), (int, float))
        ]
        complete = [
            float(row["result"]["timing"]["complete_ms"])
            for row in ok
            if isinstance(row["result"]["timing"].get("complete_ms"), (int, float))
        ]
        chars = [float(row["result"]["chars"]) for row in ok]
        scores = [
            float(judge_scores.get(case_id, {}).get(model, {}).get("total"))
            for case_id in case_ids
            if isinstance(judge_scores.get(case_id, {}).get(model, {}).get("total"), (int, float))
        ]
        output.append(
            {
                "model": model,
                "success": len(ok),
                "failure": len(current) - len(ok),
                "avg_score": round(statistics.mean(scores), 2) if scores else None,
                "min_score": round(min(scores), 1) if scores else None,
                "avg_ttft_ms": round(statistics.mean(ttft), 1) if ttft else None,
                "p50_ttft_ms": percentile(ttft, 0.5),
                "p90_ttft_ms": percentile(ttft, 0.9),
                "avg_complete_ms": round(statistics.mean(complete), 1) if complete else None,
                "p90_complete_ms": percentile(complete, 0.9),
                "avg_chars": round(statistics.mean(chars), 1) if chars else None,
                "markdown_violation_cases": sum(bool(row["result"]["markdown_flags"]) for row in ok),
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=bench.DATASET)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--ids", default=",".join(DEFAULT_IDS))
    parser.add_argument("--thinking", choices=("on", "off"), default="off")
    parser.add_argument("--effort", choices=("low", "high", "max"), default="low")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()

    if args.list_models:
        print(json.dumps({"deepseek_models": discover_deepseek_models()}, ensure_ascii=False))
        return 0

    models = [value.strip() for value in args.models.split(",") if value.strip()]
    ids = [value.strip() for value in args.ids.split(",") if value.strip()]
    all_cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = [case for case in all_cases if case["id"] in set(ids)]
    case_ids = [case["id"] for case in cases]
    prompt = args.prompt.read_text(encoding="utf-8").strip()
    prepared = prepare_cases(cases, prompt)

    rows: list[dict[str, Any]] = []
    for item in prepared:
        case = item["case"]
        for model in models:
            result = run_one(
                model,
                item,
                thinking=args.thinking == "on",
                effort=args.effort,
            )
            row = {
                "case_id": case["id"],
                "title": case["title"],
                "model": model,
                "knowledge": item["knowledge"].summary(),
                "result": result,
            }
            rows.append(row)
            print(
                json.dumps(
                    {
                        "type": "generation",
                        "case_id": case["id"],
                        "model": model,
                        "status": result["status"],
                        "chars": result["chars"],
                        "timing": result["timing"],
                        "error": result.get("error", ""),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    judge_scores: dict[str, dict[str, Any]] = {}
    if args.judge:
        judge = GeminiQuestionClient(load_google_api_key(), thinking_level="LOW")
        for case in cases:
            answers = {
                row["model"]: row["result"]["answer"]
                for row in rows
                if row["case_id"] == case["id"] and row["result"]["status"] == "ok"
            }
            if not answers:
                continue
            try:
                judge_scores[case["id"]] = judge_case(judge, case, answers)
            except Exception as error:
                judge_scores[case["id"]] = {"_error": str(error)}
            print(
                json.dumps(
                    {
                        "type": "judge",
                        "case_id": case["id"],
                        "scores": judge_scores[case["id"]],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    summary = summarize(models, case_ids, rows, judge_scores)
    detail = {
        "type": "matrix_summary",
        "models": models,
        "case_ids": case_ids,
        "thinking": args.thinking,
        "effort": args.effort,
        "summary": summary,
        "case_scores": judge_scores,
        "answers": {
            case_id: {
                row["model"]: row["result"].get("answer", "")
                for row in rows
                if row["case_id"] == case_id
            }
            for case_id in case_ids
        },
        "errors": [
            {
                "case_id": row["case_id"],
                "model": row["model"],
                "error": row["result"].get("error", ""),
            }
            for row in rows
            if row["result"]["status"] != "ok"
        ],
    }
    print(json.dumps(detail, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
