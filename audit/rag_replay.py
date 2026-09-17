#!/usr/bin/env python3
"""Bounded real-API comparison, stdout only; never prints credentials.

Fixtures below are constructed regression probes, NOT the inaccessible Sept 16
interview CSV. --live explicitly authorizes paid generation. No production config,
remote pipeline, source document or interview archive is modified by this script.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lanshot_common.knowledge import KnowledgeConfig, KnowledgeService, KnowledgeSearchResult, ground_text
from lanshot_common.interview_rag import route_question, filter_knowledge, generation_prompt

BASELINE = "c8e9e1634323a2aa2dcab744b54cc11ac2acf95f"
CASES = [
    {"id": "general_agents", "question": "什么任务适合单 Agent，什么任务适合多 Agent？", "microphone": "",
     "expected": "通用选择依据；不要擅自讲成CODA项目或把Plan等同多Agent。"},
    {"id": "general_hnsw", "question": "HNSW是按照某一个语义维度建立索引的吗？", "microphone": "",
     "previous": "系统声音识别：\nCODA 的 Plan 模式怎么运行？\n\n麦克风识别：\n",
     "expected": "说明多维向量距离和邻接图；不是按单一语义维度索引，不串入CODA。"},
    {"id": "coda_modes", "question": "CODA为什么同时做ReAct、Plan和Team？具体怎么运行，代价是什么？", "microphone": "",
     "expected": "短任务循环、DAG任务状态、独立审查；解释各自代价，不只列名字。"},
    {"id": "rag_metric", "question": "你这个EnterpriseRAG项目98.09%是答案准确率吗？", "microphone": "",
     "previous": "系统声音识别：\nCODA 的工具权限怎么做？\n\n麦克风识别：\n",
     "expected": "第一句说不是；区分Document Hit@10与答案质量；不能把不同分母拼成提升。"},
    {"id": "coda_resume", "question": "那第三个任务失败以后怎么恢复？是不是所有失败都可以直接重跑？", "microphone": "",
     "previous": "系统声音识别：\nCODA 的 Plan 模式怎么管理任务和依赖？\n\n麦克风识别：\n",
     "expected": "继承CODA追问，区分Checkpoint和文件快照，保留PARTIAL/FAILED实际边界。"},
    {"id": "personal_unknown", "question": "CODA上线后服务了多少真实用户？", "microphone": "",
     "expected": "不能编造用户数、线上规模或贡献；说明已知定位，不写大段空泛建议。"},
]


def load_module(baseline: bool):
    name = "lanshot_replay_baseline" if baseline else "lanshot_replay_candidate"
    filename = ROOT / "lanshot2/audio_service.py"
    module = types.ModuleType(name)
    module.__file__ = str(filename)
    sys.modules[name] = module
    if baseline:
        code = subprocess.run(["git", "show", BASELINE + ":lanshot2/audio_service.py"],
                              cwd=ROOT, capture_output=True, text=True, check=True).stdout
    else:
        code = filename.read_text(encoding="utf-8")
    exec(compile(code, str(filename), "exec"), module.__dict__)
    if baseline:
        # Pin the evidence formatter too: candidate changes must not leak into
        # the old arm of a live comparison.
        knowledge_name = name + "_knowledge"
        original = types.ModuleType(knowledge_name)
        original.__file__ = str(ROOT / "lanshot_common/knowledge.py")
        sys.modules[knowledge_name] = original
        knowledge_code = subprocess.run(
            ["git", "show", BASELINE + ":lanshot_common/knowledge.py"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        exec(compile(knowledge_code, original.__file__, "exec"), original.__dict__)
        module.ground_text = original.ground_text
    return module


def run_case(case, variant, module, prompt, key, config):
    started = time.monotonic()
    previous = case.get("previous", "")
    route = route_question(case["question"], case["microphone"], previous_question=previous)
    if variant == "candidate":
        prompt = generation_prompt(prompt.strip(), route)
    query = route.query if variant == "candidate" else module.retrieval_query(
        case["question"], case["microphone"], previous_question=previous)
    service = KnowledgeService(lambda: key)
    knowledge = KnowledgeSearchResult.disabled(query)
    if variant == "baseline" or route.use_knowledge:
        knowledge = service.search(query)
    selection = {}
    if variant == "candidate":
        knowledge, selection = filter_knowledge(knowledge, route)
        text, history = route.generation_input(), []
    else:
        text = module.combined_transcript(case["question"], case["microphone"])
        # No invented previous answer. Prior original question is supplied to
        # retrieval as the real baseline does; this is NOT a full-history replay.
        history = []
    grounder = module.ground_text if variant == "baseline" else ground_text
    grounded = grounder(text, knowledge)
    client = module.VoiceQuestionClient(key)
    result = {
        "case": case["id"], "variant": variant, "provenance": "constructed_probe",
        "question": case["question"], "expected": case["expected"],
        "query": query, "route": route.as_dict() if variant == "candidate" else {"kind": "always_retrieve"},
        "knowledge": knowledge.summary(), "sources": [
            {"name": h.document_name, "title": h.title, "score": h.score, "chars": len(h.text)}
            for h in knowledge.hits], "selection": selection, "input_chars": len(grounded),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "history_replay": "no_previous_model_answer_available",
    }
    generation_start = time.monotonic()
    try:
        if variant == "candidate":
            answer = client.ask_stream(grounded, prompt, history=history)
        else:
            answer = client.ask(grounded, prompt, history=history)
        result["answer"] = answer
        result["status"] = "complete"
    except Exception as error:
        # Client errors contain status codes, never keys or response bodies.
        result["status"] = "failed"
        result["error_type"] = type(error).__name__
        result["error"] = str(error) if isinstance(error, RuntimeError) else "evaluation_error"
    result["generation"] = getattr(client, "last_timing", {})
    result["usage"] = getattr(client, "last_usage", {})
    result["generation_ms"] = round((time.monotonic() - generation_start) * 1000)
    result["end_to_end_ms"] = round((time.monotonic() - started) * 1000)
    first = result["generation"].get("first_visible_ms")
    result["first_visible_total_ms"] = (
        round((generation_start - started) * 1000) + first
        if isinstance(first, (int, float)) else result["end_to_end_ms"]
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", default="general_agents,coda_modes")
    parser.add_argument("--variant", choices=("baseline", "candidate", "both"), default="both")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--max-calls", type=int, default=4)
    args = parser.parse_args()
    selected = args.case.split(",")
    known = {x["id"]: x for x in CASES}
    if any(x not in known for x in selected):
        parser.error("unknown case id")
    cases = [known[x] for x in selected]
    variants = ("baseline", "candidate") if args.variant == "both" else (args.variant,)
    if len(cases) * len(variants) > min(args.max_calls, 12):
        parser.error("generation call budget exceeded")
    if not args.live:
        print(json.dumps({"live": False, "baseline": BASELINE, "cases": cases,
                          "routes": [route_question(x["question"], x["microphone"],
                             previous_question=x.get("previous", "")).as_dict() for x in cases]}, ensure_ascii=False, indent=2))
        return 0
    modules = {v: load_module(v == "baseline") for v in variants}
    key = next(iter(modules.values())).load_api_key()
    config = KnowledgeConfig.load()
    if config.workspace_id != "llm-p9tl4xht9iosd4yq" or config.agent_id != "aid-aa4e334a258744889c83c0cfb32db676":
        raise SystemExit("Active knowledge config differs from the requested target; no calls made.")
    prompts = {}
    for variant in variants:
        prompts[variant] = (
            subprocess.run(["git", "show", BASELINE + ":lanshot2/voice_question_prompt.txt"],
                           cwd=ROOT, capture_output=True, text=True, check=True).stdout
            if variant == "baseline" else (ROOT / "lanshot2/voice_question_prompt.txt").read_text()
        )
    jobs = [(case, variant) for case in cases for variant in variants]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda pair: run_case(pair[0], pair[1], modules[pair[1]],
                            prompts[pair[1]], key, config), jobs))
    print(json.dumps({
        "baseline": BASELINE, "provenance": "constructed_probes_not_today_interviews",
        "target": {"workspace": config.workspace_id, "agent": config.agent_id,
                   "timeout_seconds": config.timeout_seconds, "max_hits": config.max_hits,
                   "min_score": config.min_score, "max_context_chars": config.max_context_chars},
        "generation_calls": len(jobs), "workers": args.workers, "results": results,
        "quality_note": "Requires human review; test assertions and keyword hits are not answer accuracy.",
    }, ensure_ascii=False, indent=2))
    return int(any(x["status"] != "complete" for x in results))


if __name__ == "__main__":
    raise SystemExit(main())
