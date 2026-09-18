#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import Queue


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from lanshot_common.interview_rag import route_question, filter_knowledge, generation_prompt  # noqa: E402
from lanshot_common.knowledge import (  # noqa: E402
    KnowledgeSearchResult,
    KnowledgeService,
    ground_text,
)


ROOT = Path(__file__).resolve().parent
APP = ROOT / "LanShot Voice Capture.app"
OVERLAY_APP = ROOT.parent / "capture-exclusion-demo/build/CaptureExclusionDemo.app"
OVERLAY_EXECUTABLE = OVERLAY_APP / "Contents/MacOS/CaptureExclusionDemo"
DEFAULT_OUTPUT = Path.home() / "Library/Application Support/LanShot2/audio"
VOICE_PROMPT = ROOT / "voice_question_prompt.txt"
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VOICE_MODEL = "glm-5.3"
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.8-flash:streamGenerateContent?alt=sse"
)


@dataclass(frozen=True)
class QuestionSnapshot:
    session_id: str
    conversation_id: str
    identifier: str
    archive_directory: Path
    system_text: str
    microphone_text: str

    @property
    def question(self) -> str:
        return combined_transcript(self.system_text, self.microphone_text)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


_PROMPT_CACHE: dict[str, tuple[float, str]] = {}


def get_voice_prompt(path: Path = VOICE_PROMPT) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    cached = _PROMPT_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        content = ""
    _PROMPT_CACHE[str(path)] = (mtime, content)
    return content


class ThrottledWriter:
    """Buffers rapid streaming string updates to reduce atomic file replacement churn."""

    def __init__(self, path: Path, min_interval: float = 0.08) -> None:
        self.path = path
        self.min_interval = min_interval
        self.last_write = 0.0
        self.last_text = ""

    def write(self, text: str) -> None:
        self.last_text = text
        now = time.monotonic()
        if now - self.last_write >= self.min_interval:
            atomic_text(self.path, f"{text}\n")
            self.last_write = now

    def flush(self) -> None:
        if self.last_text:
            atomic_text(self.path, f"{self.last_text}\n")
            self.last_write = time.monotonic()


def current_conversation_id(output: Path) -> str:
    state_file = output.parent / "current_session.json"
    try:
        value = json.loads(state_file.read_text(encoding="utf-8"))
        session_id = value.get("id", "") if isinstance(value, dict) else ""
        if isinstance(session_id, str) and 1 <= len(session_id) <= 80:
            return session_id
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def create_conversation_session(output: Path) -> dict:
    root = output.parent
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    record = {
        "id": session_id,
        "title": f"面试会话 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "created_at": now,
    }
    sessions_file = root / "sessions.jsonl"
    with sessions_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(sessions_file, 0o600)
    atomic_text(root / "current_session.json", json.dumps(record, ensure_ascii=False) + "\n")
    atomic_text(output / "current_conversation_id.txt", f"{session_id}\n")
    return record


def ensure_conversation_session(output: Path) -> str:
    session_id = current_conversation_id(output)
    if session_id:
        atomic_text(output / "current_conversation_id.txt", f"{session_id}\n")
        return session_id
    return str(create_conversation_session(output)["id"])


def activate_conversation_session(output: Path, session_id: str) -> dict:
    if not session_id or len(session_id) > 80:
        raise ValueError("invalid session id")
    record = None
    try:
        with (output.parent / "sessions.jsonl").open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("id") == session_id:
                    record = candidate
    except OSError as error:
        raise ValueError("session not found") from error
    if record is None:
        raise ValueError("session not found")
    if process_id(output):
        capture_stop(output)
    atomic_text(
        output.parent / "current_session.json",
        json.dumps(record, ensure_ascii=False) + "\n",
    )
    atomic_text(output / "current_conversation_id.txt", f"{session_id}\n")
    return record


def load_api_key() -> str:
    key = ""
    if sys.platform == "darwin":
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "com.lanshot.bailian",
                "-a",
                "DASHSCOPE_API_KEY",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        key = result.stdout.strip() if result.returncode == 0 else ""
    if key:
        return key
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未找到百炼 API Key")
    return key


def load_google_api_key() -> str:
    key = ""
    if sys.platform == "darwin":
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "com.lanshot.google",
                "-a",
                "GEMINI_API_KEY",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        key = result.stdout.strip() if result.returncode == 0 else ""
    if key:
        return key
    key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not key:
        raise RuntimeError("未找到 Gemini Developer API Key")
    return key


def google_request_opener(proxy_url: str | None = None):
    configured = (
        proxy_url
        if proxy_url is not None
        else os.environ.get("LANSHOT_GOOGLE_PROXY", "").strip()
    )
    if configured.lower() in ("direct", "none"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    if not configured:
        try:
            with socket.create_connection(("127.0.0.1", 7890), timeout=0.15):
                configured = "http://127.0.0.1:7890"
        except OSError:
            return urllib.request.urlopen
    parsed = urllib.parse.urlsplit(configured)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Google proxy must be an HTTP URL")
    if parsed.username or parsed.password:
        raise ValueError("Google proxy credentials are not supported")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": configured, "https": configured})
    ).open


class GeminiQuestionClient:
    def __init__(
        self,
        api_key: str,
        *,
        opener=None,
        url: str = GEMINI_URL,
        model: str = GEMINI_MODEL,
        thinking_level: str = "MEDIUM",
    ) -> None:
        if not api_key.strip():
            raise ValueError("Google API key is empty")
        if thinking_level not in ("LOW", "MEDIUM", "HIGH"):
            raise ValueError("unsupported Gemini thinking level")
        self.api_key = api_key.strip()
        self.opener = opener or google_request_opener()
        self.url = url
        self.model = model
        self.thinking_level = thinking_level
        self.last_usage: dict = {}
        self.last_timing: dict = {}

    def ask(self, question: str, prompt: str, history: list[dict] | None = None) -> str:
        return self.ask_stream(question, prompt, history=history)

    def ask_stream(
        self,
        question: str,
        prompt: str,
        history: list[dict] | None = None,
        *,
        on_update=None,
    ) -> str:
        contents = []
        for item in history or []:
            contents.append({"role": "user", "parts": [{"text": item["input"]}]})
            contents.append({"role": "model", "parts": [{"text": item["answer"]}]})
        contents.append({"role": "user", "parts": [{"text": question}]})
        payload = {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 4_096,
                "thinkingConfig": {"thinkingLevel": self.thinking_level},
            },
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        first_visible_ms = None
        answer = ""
        usage: dict = {}
        event_count = 0
        received_bytes = 0
        last_update = 0.0
        try:
            with self.opener(request, timeout=20) as response:
                for raw_line in response:
                    received_bytes += len(raw_line)
                    if received_bytes > 4 * 1024 * 1024:
                        raise RuntimeError("Gemini 返回内容过大")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    encoded_event = line[5:].strip()
                    if not encoded_event or encoded_event == "[DONE]":
                        continue
                    try:
                        event = json.loads(encoded_event)
                    except json.JSONDecodeError as error:
                        raise RuntimeError("Gemini 返回了无效流数据") from error
                    if not isinstance(event, dict):
                        raise RuntimeError("Gemini 返回了无效流事件")
                    event_count += 1
                    candidate_usage = event.get("usageMetadata", {})
                    if isinstance(candidate_usage, dict) and candidate_usage:
                        usage = candidate_usage
                    for candidate in event.get("candidates") or []:
                        if not isinstance(candidate, dict):
                            continue
                        content = candidate.get("content", {})
                        parts = content.get("parts", []) if isinstance(content, dict) else []
                        for part in parts:
                            if not isinstance(part, dict) or part.get("thought"):
                                continue
                            text = part.get("text", "")
                            if not isinstance(text, str) or not text:
                                continue
                            if first_visible_ms is None:
                                first_visible_ms = round((time.monotonic() - started) * 1000)
                            answer += text
                            now = time.monotonic()
                            if on_update is not None and (not last_update or now - last_update >= 0.06):
                                on_update(answer.strip())
                                last_update = now
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"Gemini 请求失败，HTTP {status}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RuntimeError("无法连接 Gemini 服务") from error
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("Gemini 没有返回答案")
        if on_update is not None:
            on_update(answer.strip())
        self.last_usage = usage
        self.last_timing = {
            "provider": "gemini_developer_api",
            "streaming": True,
            "thinking_level": self.thinking_level,
            "first_visible_ms": first_visible_ms,
            "complete_ms": round((time.monotonic() - started) * 1000),
            "event_count": event_count,
        }
        return answer.strip()


class VoiceQuestionClient:
    def __init__(
        self,
        api_key: str,
        *,
        opener=urllib.request.urlopen,
        url: str = BAILIAN_URL,
        model: str = VOICE_MODEL,
    ) -> None:
        self.api_key = api_key
        self.opener = opener
        self.url = url
        self.model = model
        self.last_timing: dict = {}

    def ask(self, question: str, prompt: str, history: list[dict] | None = None) -> str:
        started = time.monotonic()
        messages = [{"role": "system", "content": prompt}]
        for item in history or []:
            messages.append({"role": "user", "content": item["input"]})
            messages.append({"role": "assistant", "content": item["answer"]})
        messages.append({"role": "user", "content": question})
        payload = {
            "model": self.model,
            "messages": messages,
            "enable_thinking": True,
            "reasoning_effort": "low",
            "stream": False,
            "max_tokens": 1600,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=30) as response:
                body = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise RuntimeError(f"百炼请求失败，HTTP {status}") from error
        except urllib.error.URLError as error:
            raise RuntimeError("无法连接百炼服务") from error
        try:
            result = json.loads(body)
            answer = result["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError("百炼返回了无效结果") from error
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("大模型没有返回答案")
        self.last_timing = {
            "provider": "bailian_glm",
            "streaming": False,
            "thinking_level": "low",
            "complete_ms": round((time.monotonic() - started) * 1000),
        }
        return answer.strip().replace("```python", "").replace("```", "").strip()

    def ask_stream(self, question: str, prompt: str, history: list[dict] | None = None, *, on_update=None) -> str:
        from lanshot_common.bailian_stream import ask_stream
        return ask_stream(self, question, prompt, history, on_update=on_update)


class FallbackQuestionClient:
    def __init__(self, primary: GeminiQuestionClient, fallback: VoiceQuestionClient) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model
        self.fallback_used = False
        self.last_timing: dict = {}
        self.last_usage: dict = {}

    def ask(self, question: str, prompt: str, history: list[dict] | None = None) -> str:
        return self.ask_stream(question, prompt, history=history)

    def ask_stream(
        self,
        question: str,
        prompt: str,
        history: list[dict] | None = None,
        *,
        on_update=None,
    ) -> str:
        self.model = self.primary.model
        self.fallback_used = False
        started = time.monotonic()
        try:
            answer = self.primary.ask_stream(
                question,
                prompt,
                history=history,
                on_update=on_update,
            )
            self.last_timing = self.primary.last_timing
            self.last_usage = self.primary.last_usage
            return answer
        except RuntimeError:
            self.model = self.fallback.model
            self.fallback_used = True
            answer = self.fallback.ask(question, prompt, history=history)
            self.last_timing = {
                "provider": "bailian_fallback",
                "streaming": False,
                "complete_ms": round((time.monotonic() - started) * 1000),
            }
            self.last_usage = {}
            if on_update is not None:
                on_update(answer)
            return answer


QuestionClient = VoiceQuestionClient | GeminiQuestionClient | FallbackQuestionClient


def default_question_client() -> QuestionClient:
    return VoiceQuestionClient(load_api_key())


def combined_transcript(system_text: str, microphone_text: str) -> str:
    return (
        "系统声音识别：\n"
        f"{system_text or '（未识别到内容）'}\n\n"
        "麦克风识别：\n"
        f"{microphone_text or '（未识别到内容）'}"
    )


def retrieval_query(
    system_text: str,
    microphone_text: str,
    *,
    previous_question: str = "",
) -> str:
    """Compatibility wrapper for the shared retrieval/generation turn router."""
    return route_question(
        system_text, microphone_text, previous_question=previous_question,
    ).query


def load_question_history(
    output: Path,
    limit: int = 6,
    *,
    conversation_id: str | None = None,
) -> list[dict]:
    history_file = output.parent / "conversation_history.jsonl"
    active_conversation = conversation_id or ensure_conversation_session(output)
    items: deque[dict] = deque(maxlen=limit)
    try:
        with history_file.open("r", encoding="utf-8") as stream:
            for line in stream:
                if len(line) > 64 * 1024:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("conversation_id") == active_conversation
                    and isinstance(item.get("input"), str)
                    and isinstance(item.get("answer"), str)
                ):
                    items.append(item)
    except OSError:
        pass
    return list(items)


def append_question_history(
    output: Path,
    question: str,
    answer: str,
    *,
    capture_session_id: str | None = None,
    conversation_id: str | None = None,
    knowledge: dict | None = None,
    model: str | None = None,
    generation: dict | None = None,
) -> None:
    history_file = output.parent / "conversation_history.jsonl"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "capture_session_id": capture_session_id or read_text(output / "capture_session_id.txt"),
        "conversation_id": conversation_id or ensure_conversation_session(output),
        "at": time.time(),
        "input": question,
        "answer": answer,
    }
    if knowledge is not None:
        item["knowledge"] = knowledge
    if model:
        item["model"] = model
    if generation:
        item["generation"] = generation
    with history_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(history_file, 0o600)


def prepare_question_snapshot(output: Path) -> QuestionSnapshot:
    session_id = read_text(output / "capture_session_id.txt")
    system_text = read_text(output / "interviewer.txt")
    microphone_text = read_text(output / "me.txt")
    question = combined_transcript(system_text, microphone_text)
    archive_directory = archive_capture(output, question, "正在生成答案...")
    return QuestionSnapshot(
        session_id=session_id,
        conversation_id=ensure_conversation_session(output),
        identifier=capture_identifier(session_id),
        archive_directory=archive_directory,
        system_text=system_text,
        microphone_text=microphone_text,
    )


def update_snapshot_archive(snapshot: QuestionSnapshot, answer: str) -> None:
    atomic_text(
        snapshot.archive_directory / f"{snapshot.identifier}-question.txt",
        f"{snapshot.question}\n",
    )
    atomic_text(
        snapshot.archive_directory / f"{snapshot.identifier}-answer.txt",
        f"{answer}\n",
    )


def record_snapshot_knowledge(
    output: Path,
    snapshot: QuestionSnapshot,
    result: KnowledgeSearchResult,
    *,
    routing: dict | None = None,
    selection: dict | None = None,
) -> None:
    payload = {
        "mode": "voice",
        "routing": routing or {},
        "selection": selection or {},
        "capture_session_id": snapshot.session_id,
        "conversation_id": snapshot.conversation_id,
        "at": time.time(),
        **result.as_dict(),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_text(output / "knowledge_status.json", serialized)
    atomic_text(
        snapshot.archive_directory / f"{snapshot.identifier}-knowledge.json",
        serialized,
    )


def submit_snapshot(
    output: Path,
    snapshot: QuestionSnapshot,
    *,
    client: QuestionClient | None = None,
    knowledge_service: KnowledgeService | None = None,
) -> bool:
    started = time.monotonic()
    question = snapshot.question
    if not snapshot.system_text and not snapshot.microphone_text:
        answer = "两路都没有识别到文字，请重新采集。"
        atomic_text(output / "answer.txt", f"{answer}\n")
        update_snapshot_archive(snapshot, answer)
        return False
    atomic_text(output / "question.txt", f"{question}\n")
    atomic_text(output / "answer.txt", "正在生成答案...\n")
    atomic_text(
        output / "question_status.json",
        json.dumps(
            {"status": "retrieving", "session_id": snapshot.session_id, "at": time.time()},
            ensure_ascii=False,
        )
        + "\n",
    )
    history = load_question_history(output, limit=1, conversation_id=snapshot.conversation_id)
    previous_question = history[-1]["input"] if history else ""
    route = route_question(
        snapshot.system_text, snapshot.microphone_text,
        previous_question=previous_question,
    )
    search_query = route.query
    knowledge = KnowledgeSearchResult.disabled(search_query)
    if knowledge_service is not None and route.use_knowledge:
        try:
            knowledge = knowledge_service.search(search_query)
        except Exception:
            knowledge = KnowledgeSearchResult.failed(search_query, "retrieval_exception")
    knowledge, selection = filter_knowledge(knowledge, route)
    record_snapshot_knowledge(
        output, snapshot, knowledge, routing=route.as_dict(), selection=selection,
    )
    if route.kind == "incomplete":
        answer = "这段还没有完整问题，请补充问题后再提交。"
        atomic_text(output / "answer.txt", f"{answer}\n")
        atomic_text(output / "question_status.json", json.dumps(
            {"status": "incomplete", "routing": route.as_dict(), "at": time.time()},
            ensure_ascii=False,
        ) + "\n")
        update_snapshot_archive(snapshot, answer)
        return False
    atomic_text(
        output / "question_status.json",
        json.dumps(
            {
                "status": "requesting",
                "session_id": snapshot.session_id,
                "knowledge": knowledge.summary(),
                "at": time.time(),
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    try:
        prompt = generation_prompt(get_voice_prompt(), route)
        active_client = client or default_question_client()
        grounded_question = ground_text(route.generation_input(), knowledge)
        # Generated answers are not verified history. Only the previous original
        # interviewer turn is carried in route.generation_input() for follow-ups.
        history = []
        generation_started_ms = round((time.monotonic() - started) * 1000)
        streaming_ask = getattr(active_client, "ask_stream", None)
        throttled_writer = ThrottledWriter(output / "answer.txt", min_interval=0.08)
        if callable(streaming_ask):
            try:
                answer = streaming_ask(
                    grounded_question,
                    prompt,
                    history=history,
                    on_update=throttled_writer.write,
                )
            finally:
                throttled_writer.flush()
        else:
            answer = active_client.ask(grounded_question, prompt, history=history)
    except (OSError, RuntimeError) as error:
        message = f"提问失败：{error}"
        atomic_text(output / "answer.txt", f"{message}\n")
        atomic_text(
            output / "question_status.json",
            json.dumps(
                {
                    "status": "failed",
                    "message": str(error),
                    "knowledge": knowledge.summary(),
                    "at": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n",
        )
        update_snapshot_archive(snapshot, message)
        return False
    atomic_text(output / "answer.txt", f"{answer}\n")
    model_used = getattr(active_client, "model", VOICE_MODEL)
    fallback_used = bool(getattr(active_client, "fallback_used", False))
    generation = dict(getattr(active_client, "last_timing", {}))
    generation.update({
        "retrieval_ms": knowledge.elapsed_ms,
        "input_chars": len(grounded_question),
        "route": route.kind,
        "end_to_end_ms": round((time.monotonic() - started) * 1000),
    })
    if isinstance(generation.get("first_visible_ms"), (int, float)):
        generation["first_visible_total_ms"] = generation_started_ms + generation["first_visible_ms"]
    atomic_text(
        output / "question_status.json",
        json.dumps(
            {
                "status": "complete",
                "model": model_used,
                "fallback_used": fallback_used,
                "generation": generation,
                "session_id": snapshot.session_id,
                "knowledge": knowledge.summary(),
                "at": time.time(),
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    append_question_history(
        output,
        question,
        answer,
        capture_session_id=snapshot.session_id,
        conversation_id=snapshot.conversation_id,
        knowledge=knowledge.summary(),
        model=model_used,
        generation=generation,
    )
    update_snapshot_archive(snapshot, answer)
    return True


def submit_question(
    output: Path,
    *,
    client: QuestionClient | None = None,
) -> bool:
    return submit_snapshot(output, prepare_question_snapshot(output), client=client)


def capture_identifier(configured_id: str) -> str:
    identifier = "".join(
        character for character in configured_id if character.isalnum() or character in ("-", "_")
    )[:80]
    return identifier or f"{time.strftime('%H%M%S')}-{time.time_ns()}"


def archive_capture(output: Path, question: str, answer: str) -> Path:
    configured_id = read_text(output / "capture_session_id.txt")
    identifier = capture_identifier(configured_id)
    history = output.parent / "questions" / time.strftime("%Y-%m-%d")
    history.mkdir(parents=True, exist_ok=True)
    atomic_text(history / f"{identifier}-question.txt", f"{question}\n")
    atomic_text(history / f"{identifier}-answer.txt", f"{answer}\n")
    for name in ("interviewer.wav", "me.wav", "interviewer.txt", "me.txt"):
        source = output / name
        if source.is_file():
            shutil.copy2(source, history / f"{identifier}-{name}")
    atomic_text(output / "last_archived_session_id.txt", f"{configured_id}\n")
    return history


def save_unsubmitted_transcript_to_history(
    output: Path,
    default_answer: str = "（本轮录音已停止，未提交模型，录音转写已完整存盘）",
) -> bool:
    system_text = read_text(output / "interviewer.txt")
    microphone_text = read_text(output / "me.txt")
    if not system_text and not microphone_text:
        return False
    session_id = read_text(output / "capture_session_id.txt")
    conversation_id = ensure_conversation_session(output)

    history_file = output.parent / "conversation_history.jsonl"
    if history_file.is_file() and session_id:
        try:
            with history_file.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if f'"capture_session_id": "{session_id}"' in line:
                        return False
        except OSError:
            pass

    question = combined_transcript(system_text, microphone_text)
    append_question_history(
        output,
        question,
        default_answer,
        capture_session_id=session_id,
        conversation_id=conversation_id,
    )
    return True


def archive_pending_capture(output: Path) -> None:
    session_id = read_text(output / "capture_session_id.txt")
    if not session_id or session_id == read_text(output / "last_archived_session_id.txt"):
        return
    if not any((output / name).is_file() for name in ("interviewer.wav", "me.wav")):
        return
    save_unsubmitted_transcript_to_history(output)
    archive_capture(
        output,
        combined_transcript(
            read_text(output / "interviewer.txt"),
            read_text(output / "me.txt"),
        ),
        read_text(output / "answer.txt") or "本轮因中断保存，尚未生成答案。",
    )


def tracked_process_id(output: Path, filename: str) -> int | None:
    raw = read_text(output / filename)
    try:
        pid = int(raw)
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def process_id(output: Path) -> int | None:
    return tracked_process_id(output, "capture.pid")


def overlay_process_id(output: Path) -> int | None:
    return tracked_process_id(output, "voice_overlay.pid")


def control_process_id(output: Path) -> int | None:
    return tracked_process_id(output, "voice_control.pid")


def write_capture_command(output: Path, action: str) -> None:
    if action not in (
        "start",
        "stop",
        "submit",
        "new-session",
        "switch-screenshot",
        "shutdown",
        "quit",
    ):
        raise ValueError("unsupported capture command")
    command = output / "voice_capture_command.txt"
    temporary = command.with_suffix(".tmp")
    temporary.write_text(f"{action} {time.time_ns()}\n", encoding="utf-8")
    os.replace(temporary, command)


def start_control(output: Path) -> None:
    if control_process_id(output):
        return
    for name in ("voice_control.pid", "voice_hotkey_status.txt"):
        try:
            (output / name).unlink()
        except FileNotFoundError:
            pass
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "control-loop", "--output", str(output)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if control_process_id(output):
            return
        time.sleep(0.1)
    raise RuntimeError("语音采集控制器启动超时")


def stop_control(output: Path) -> bool:
    pid = control_process_id(output)
    if not pid:
        return True
    write_capture_command(output, "quit")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not control_process_id(output):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not control_process_id(output):
            return True
        time.sleep(0.1)
    return False


def start_overlay(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "voice_overlay_launch.lock").open("a+") as launch_lock:
        fcntl.flock(launch_lock.fileno(), fcntl.LOCK_EX)
        if overlay_process_id(output):
            return
        if not OVERLAY_EXECUTABLE.is_file():
            raise FileNotFoundError(f"缺少语音悬浮窗：{OVERLAY_EXECUTABLE}")
        for name in ("voice_overlay.pid", "voice_overlay_status.json"):
            try:
                (output / name).unlink()
            except FileNotFoundError:
                pass
        subprocess.run(
            [
                "open",
                "-n",
                str(OVERLAY_APP),
                "--args",
                "--lanshot-voice-dir",
                str(output),
            ],
            check=True,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if overlay_process_id(output):
                return
            time.sleep(0.1)
        raise RuntimeError("语音悬浮窗启动超时")


def stop_overlay(output: Path) -> bool:
    pid = overlay_process_id(output)
    if not pid:
        return True
    command = output / "voice_overlay_command.txt"
    temporary = command.with_suffix(".tmp")
    temporary.write_text(f"quit {time.time_ns()}\n", encoding="utf-8")
    os.replace(temporary, command)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not overlay_process_id(output):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not overlay_process_id(output):
            return True
        time.sleep(0.1)
    return False


def prepare(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    ensure_conversation_session(output)
    if not process_id(output) and read_text(output / "capture.log") in ("", "running", "starting"):
        (output / "capture.log").write_text("stopped\n", encoding="utf-8")
    try:
        start_control(output)
        start_overlay(output)
        write_capture_command(output, "start")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        stop_control(output)
        print(str(error), file=sys.stderr)
        return 1
    print("语音模式已就绪，已自动开始双路音频采集")
    return 0


def stop_capture(output: Path) -> bool:
    pid = process_id(output)
    if not pid:
        (output / "capture.log").write_text("stopped\n", encoding="utf-8")
        return True
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process_id(output):
        time.sleep(0.2)
    stopped = process_id(output) is None
    if stopped:
        (output / "capture.log").write_text("stopped\n", encoding="utf-8")
    return stopped


def start(output: Path, *, ensure_controller: bool = True) -> int:
    if ensure_controller:
        try:
            start_control(output)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if process_id(output):
        try:
            start_overlay(output)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print("音频采集和语音悬浮窗已经在运行")
        return 0
    executable = APP / "Contents/MacOS/native_audio_capture"
    if not executable.exists():
        print(f"缺少本地采集程序：{executable}", file=sys.stderr)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    ensure_conversation_session(output)
    archive_pending_capture(output)
    session_id = f"{time.strftime('%H%M%S')}-{time.time_ns()}"
    atomic_text(output / "capture_session_id.txt", f"{session_id}\n")
    if not (output / "answer.txt").is_file():
        atomic_text(output / "answer.txt", "等待发送问题...\n")
    for name in ("capture.log", "capture.pid"):
        try:
            (output / name).unlink()
        except FileNotFoundError:
            pass
    subprocess.run(
        ["open", "-n", str(APP), "--args", str(output)],
        check=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = read_text(output / "capture.log")
        if state == "running":
            time.sleep(0.5)
            if not process_id(output):
                print("音频辅助进程启动后意外退出", file=sys.stderr)
                return 1
            try:
                start_overlay(output)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                audio_pid = process_id(output)
                if audio_pid:
                    os.kill(audio_pid, signal.SIGTERM)
                print(str(error), file=sys.stderr)
                return 1
            print(f"LanShot2 双路采集、实时识别和悬浮字幕已启动：{output}")
            return 0
        if state.startswith("failed:"):
            print(state, file=sys.stderr)
            if "TCC" in state:
                subprocess.run(
                    [
                        "open",
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
                    ],
                    check=False,
                )
                print("请允许 LanShot Audio Capture 的屏幕与系统音频录制权限。", file=sys.stderr)
            return 1
        time.sleep(0.25)
    print("启动超时，请检查 macOS 权限提示。", file=sys.stderr)
    return 1


def stop(output: Path) -> int:
    was_running = process_id(output) is not None
    control_stopped = stop_control(output)
    audio_stopped = stop_capture(output)
    overlay_stopped = stop_overlay(output)
    if not control_stopped or not audio_stopped or not overlay_stopped:
        print("停止超时", file=sys.stderr)
        return 1
    if was_running:
        save_unsubmitted_transcript_to_history(output)
        archive_capture(
            output,
            combined_transcript(
                read_text(output / "interviewer.txt"),
                read_text(output / "me.txt"),
            ),
            "程序退出前已保存，本轮未提交。",
        )
    archive_pending_capture(output)
    print("音频采集和语音悬浮窗已停止，文件已写完")
    return 0


def capture_stop(output: Path) -> int:
    was_running = process_id(output) is not None
    if not stop_capture(output):
        print("停止采集超时", file=sys.stderr)
        return 1
    if was_running:
        save_unsubmitted_transcript_to_history(output)
        message = "本轮采集已停止并保存，按 F22 可以发送问题。"
        archive_capture(
            output,
            combined_transcript(
                read_text(output / "interviewer.txt"),
                read_text(output / "me.txt"),
            ),
            message,
        )
        print("采集已停止并保存，未提交问题，悬浮窗继续保留")
    else:
        print("当前没有正在进行的采集")
    return 0


def capture_submit(
    output: Path,
    *,
    submission_queue: Queue[QuestionSnapshot | None] | None = None,
) -> int:
    if process_id(output) and not stop_capture(output):
        print("停止识别超时，拒绝发送不完整问题", file=sys.stderr)
        return 1
    snapshot = prepare_question_snapshot(output)
    atomic_text(output / "capture.log", "submitting\n")
    atomic_text(output / "question.txt", f"{snapshot.question}\n")
    atomic_text(output / "answer.txt", "正在生成答案...\n")
    atomic_text(
        output / "question_status.json",
        json.dumps(
            {"status": "queued", "session_id": snapshot.session_id, "at": time.time()},
            ensure_ascii=False,
        )
        + "\n",
    )
    if submission_queue is not None:
        submission_queue.put(snapshot)
    latest_action = read_text(output / "voice_capture_command.txt").split(maxsplit=1)[0]
    resumed = latest_action == "submit" and start(output, ensure_controller=False) == 0
    submitted = True
    if submission_queue is None:
        submitted = submit_snapshot(
            output,
            snapshot,
            knowledge_service=KnowledgeService(load_api_key),
        )
    if not resumed:
        atomic_text(output / "capture.log", "stopped\n")
    if resumed:
        print("问题已进入后台生成，下一轮采集已自动开始")
    else:
        print("问题已进入后台生成，悬浮窗继续运行")
    return 0 if submitted else 1


def shutdown_from_overlay(output: Path) -> bool:
    was_running = process_id(output) is not None
    audio_stopped = stop_capture(output)
    if audio_stopped and was_running:
        save_unsubmitted_transcript_to_history(output)
        archive_capture(
            output,
            combined_transcript(
                read_text(output / "interviewer.txt"),
                read_text(output / "me.txt"),
            ),
            "退出前已保存，本轮未提交。",
        )
    elif audio_stopped:
        archive_pending_capture(output)
    overlay_stopped = stop_overlay(output)
    return audio_stopped and overlay_stopped


def start_new_conversation(output: Path) -> dict:
    if process_id(output):
        capture_stop(output)
    return create_conversation_session(output)


def spawn_mode_switch(output: Path, mode: str) -> None:
    if mode not in ("screenshot", "voice"):
        raise ValueError("unsupported mode")
    controller = ROOT.parent / "unified/mode_controller.py"
    log_file = output.parent / "mode-switch.log"
    with log_file.open("ab") as stream:
        subprocess.Popen(
            [sys.executable, str(controller), mode],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def control_loop(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    pid_url = output / "voice_control.pid"
    command_url = output / "voice_capture_command.txt"
    stopping = False
    hotkey_stop = threading.Event()

    screenshot_sender = ROOT.parent / "screenshot-sender"
    sys.path.insert(0, str(screenshot_sender))
    from sender_service import MacF24Listener

    hotkey_listener = MacF24Listener()
    submissions: Queue[QuestionSnapshot | None] = Queue()
    knowledge_service = KnowledgeService(load_api_key)

    def process_questions() -> None:
        while True:
            snapshot = submissions.get()
            try:
                if snapshot is None:
                    return
                submit_snapshot(output, snapshot, knowledge_service=knowledge_service)
            except Exception as error:
                message = f"提问失败：{type(error).__name__}"
                atomic_text(output / "answer.txt", f"{message}\n")
                atomic_text(
                    output / "question_status.json",
                    json.dumps(
                        {
                            "status": "failed",
                            "session_id": snapshot.session_id if snapshot else "",
                            "message": type(error).__name__,
                            "at": time.time(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                )
                if snapshot is not None:
                    update_snapshot_archive(snapshot, message)
            finally:
                submissions.task_done()

    question_thread = threading.Thread(
        target=process_questions,
        name="lanshot-question-worker",
        daemon=True,
    )
    question_thread.start()

    def submit_capture() -> None:
        if read_text(output / "capture.log") in ("submitting", "stopping"):
            return
        atomic_text(output / "capture.log", "submitting\n")
        write_capture_command(output, "submit")

    def page_overlay(direction: str) -> None:
        atomic_text(
            output / "voice_overlay_command.txt",
            f"{direction} {time.time_ns()}\n",
        )

    def page_up() -> None:
        page_overlay("up")

    def page_down() -> None:
        page_overlay("down")

    def ignore_hotkey() -> None:
        return

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    pid_url.write_text(f"{os.getpid()}\n", encoding="utf-8")
    last_command = read_text(command_url)
    hotkey_thread = threading.Thread(
        target=hotkey_listener.run,
        args=(
            hotkey_stop,
            submit_capture,
            page_up,
            page_down,
            ignore_hotkey,
            ignore_hotkey,
        ),
        name="lanshot-voice-f22",
        daemon=True,
    )
    hotkey_thread.start()
    hotkey_deadline = time.monotonic() + 3
    while (
        time.monotonic() < hotkey_deadline
        and not hotkey_listener.ready.is_set()
        and hotkey_listener.failure_code is None
        and hotkey_thread.is_alive()
    ):
        time.sleep(0.05)
    hotkey_status = "ready" if hotkey_listener.ready.is_set() else (
        hotkey_listener.failure_code or "listener_stopped"
    )
    (output / "voice_hotkey_status.txt").write_text(f"{hotkey_status}\n", encoding="utf-8")
    next_overlay_check = time.monotonic()
    try:
        while not stopping:
            command = read_text(command_url)
            if command and command != last_command:
                last_command = command
                action = command.split(maxsplit=1)[0]
                if action == "start":
                    start(output, ensure_controller=False)
                elif action == "stop":
                    capture_stop(output)
                elif action == "submit":
                    capture_submit(output, submission_queue=submissions)
                elif action == "new-session":
                    start_new_conversation(output)
                elif action == "activate-session":
                    parts = command.split(maxsplit=2)
                    if len(parts) == 3:
                        activate_conversation_session(output, parts[1])
                elif action == "switch-screenshot":
                    spawn_mode_switch(output, "screenshot")
                elif action == "shutdown":
                    shutdown_from_overlay(output)
                    break
                elif action == "quit":
                    break
            if time.monotonic() >= next_overlay_check:
                next_overlay_check = time.monotonic() + 1
                if not overlay_process_id(output):
                    try:
                        start_overlay(output)
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        pass
            time.sleep(0.1)
    finally:
        submissions.put(None)
        question_thread.join(timeout=1)
        hotkey_stop.set()
        hotkey_listener.stop()
        hotkey_thread.join(timeout=2)
        (output / "voice_hotkey_status.txt").write_text("stopped\n", encoding="utf-8")
        if read_text(pid_url) == str(os.getpid()):
            pid_url.unlink(missing_ok=True)
    return 0


def status(output: Path) -> int:
    pid = process_id(output)
    overlay_pid = overlay_process_id(output)
    control_pid = control_process_id(output)
    state = read_text(output / "capture.log") or "未启动"
    if state == "running" and not pid:
        state = "failed: process exited unexpectedly"
    print(f"状态：{state}")
    print(f"进程：{pid if pid else '无'}")
    print(f"悬浮窗：{overlay_pid if overlay_pid else '无'}")
    print(f"控制器：{control_pid if control_pid else '无'}")
    print(f"目录：{output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LanShot2 双路实时音频采集和语音识别")
    commands = (
        "prepare",
        "start",
        "capture-stop",
        "capture-submit",
        "stop",
        "status",
        "control-loop",
    )
    parser.add_argument("command", choices=commands)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return {
        "prepare": prepare,
        "start": start,
        "capture-stop": capture_stop,
        "capture-submit": capture_submit,
        "stop": stop,
        "status": status,
        "control-loop": control_loop,
    }[args.command](
        args.output.expanduser().resolve()
    )


if __name__ == "__main__":
    raise SystemExit(main())
