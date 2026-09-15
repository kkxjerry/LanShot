#!/usr/bin/env python3
"""Small LAN receiver for LanShot senders."""

from __future__ import annotations

from reliability import FileMutex
from receiver_runtime import ReceiverRuntime

import argparse
import base64
import json
import logging
import math
import hmac
import signal
import sqlite3
import os
import re
import sys
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from lanshot_common.knowledge import (  # noqa: E402
    KnowledgeSearchResult,
    KnowledgeService,
    ground_text,
)


from reliability import (BUILD, PROTOCOL, TaskStore, QueueFull, Conflict, LeaseLost, InstanceLock,
                         Heartbeat, RetryDecision, retry_decision, error_info, trace,
                         atomic_json, atomic_write, private_dir)

LOGGER = logging.getLogger("lanshot-receiver")
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024
DEFAULT_IMAGE_PATH = Path.home() / ".local" / "share" / "lanshot-receiver" / "latest.jpg"
DEFAULT_ANSWER_PATH = Path.home() / ".local" / "share" / "lanshot-receiver" / "latest.txt"
DEFAULT_HISTORY_DIR = Path.home() / ".local" / "share" / "lanshot-receiver" / "history"
DEFAULT_PROMPT_PATH = Path(__file__).with_name("interview_prompt.txt")
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "kimi-k2.7-code"
FALLBACK_PROMPT = "识别截图中的题目并直接给出简洁、准确的纯文本答案。"
SPEECH_MARKER = "【语音稿】"
SPEECH_PATTERN = re.compile(r"<speech>\s*(.*?)\s*</speech>", re.IGNORECASE | re.DOTALL)
DEFAULT_OCR_EXECUTABLE = Path(__file__).with_name("native_ocr")


def extract_speech_text(answer: str) -> str:
    tagged = SPEECH_PATTERN.search(answer)
    if tagged and tagged.group(1).strip():
        return tagged.group(1).strip()
    _, marker, speech = answer.partition(SPEECH_MARKER)
    normalized = speech.strip() if marker else answer.strip()
    return normalized or answer.strip()


def extract_speech_segments(answer: str) -> list[str]:
    speech = extract_speech_text(answer)
    segments = [segment.strip() for segment in re.split(r"\n\s*\n+", speech)]
    return [segment for segment in segments if segment]


def load_default_prompt() -> str:
    environment_prompt = os.environ.get("LANSHOT_PROMPT", "").strip()
    if environment_prompt:
        return environment_prompt
    try:
        prompt = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as error:
        LOGGER.warning("cannot read prompt file %s: %s", DEFAULT_PROMPT_PATH, error)
        return FALLBACK_PROMPT
    return prompt or FALLBACK_PROMPT


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LanShot</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f5f7; color: #17191c; }
    header { height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; background: #fff; border-bottom: 1px solid #dfe2e6; }
    h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: 0; }
    button { min-height: 38px; padding: 0 16px; border: 0; border-radius: 6px; background: #1769e0; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
    button:disabled { background: #8a929e; cursor: wait; }
    main { width: min(1180px, 100%); margin: 0 auto; padding: 20px; }
    .status { min-height: 24px; margin: 0 0 12px; color: #59616c; font-size: 14px; }
    .viewer { min-height: 280px; display: grid; place-items: center; overflow: hidden; border: 1px solid #dfe2e6; border-radius: 6px; background: #fff; }
    img { display: block; width: 100%; height: auto; max-height: calc(100vh - 132px); object-fit: contain; }
    .empty { color: #737b86; }
    .analysis { margin-top: 16px; padding: 16px; border: 1px solid #dfe2e6; border-radius: 6px; background: #fff; }
    h2 { margin: 0 0 10px; font-size: 16px; letter-spacing: 0; }
    pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 14px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #31363d; }
    @media (max-width: 600px) { header { padding: 0 14px; } main { padding: 14px; } .viewer { min-height: 220px; } }
  </style>
</head>
<body>
  <header><h1>LanShot</h1><button id="capture" type="button">立即截图</button></header>
  <main>
    <p class="status" id="status">准备就绪</p>
    <div class="viewer"><span class="empty" id="empty">暂无截图</span><img id="image" alt="最新截图" hidden></div>
    <section class="analysis"><h2>AI 分析</h2><pre id="answer">等待截图</pre></section>
  </main>
  <script>
    const button = document.getElementById('capture');
    const statusText = document.getElementById('status');
    const image = document.getElementById('image');
    const empty = document.getElementById('empty');
    const answer = document.getElementById('answer');

    async function showLatest() {
      const response = await fetch('/latest.jpg?t=' + Date.now(), {cache: 'no-store'});
      if (!response.ok) return;
      image.src = URL.createObjectURL(await response.blob());
      image.hidden = false;
      empty.hidden = true;
    }

    async function waitForTask(id) {
      const deadline = Date.now() + 70000;
      while (Date.now() < deadline) {
        const response = await fetch('/api/tasks/' + id, {cache: 'no-store'});
        const task = await response.json();
        if (task.status === 'complete') { await showLatest(); await waitForAnalysis(); return; }
        if (task.status === 'failed') throw new Error(task.message || '截图失败');
        await new Promise(resolve => setTimeout(resolve, 700));
      }
      throw new Error('等待截图超时');
    }

    async function refreshAnalysis() {
      const response = await fetch('/api/analysis', {cache: 'no-store'});
      if (!response.ok) return null;
      const analysis = await response.json();
      if (analysis.status === 'processing') answer.textContent = '正在分析...';
      if (analysis.status === 'complete') answer.textContent = analysis.answer;
      if (analysis.status === 'failed') answer.textContent = analysis.message || 'AI 分析失败';
      if (analysis.status === 'unconfigured') answer.textContent = 'AI 尚未配置';
      return analysis;
    }

    async function waitForAnalysis() {
      const deadline = Date.now() + 180000;
      while (Date.now() < deadline) {
        const analysis = await refreshAnalysis();
        if (analysis && analysis.status === 'complete') return;
        if (analysis && analysis.status === 'failed') throw new Error(analysis.message || 'AI 分析失败');
        if (analysis && analysis.status === 'unconfigured') return;
        await new Promise(resolve => setTimeout(resolve, 900));
      }
      throw new Error('等待 AI 分析超时');
    }

    button.addEventListener('click', async () => {
      button.disabled = true;
      statusText.textContent = '正在截图...';
      try {
        const response = await fetch('/api/capture', {method: 'POST'});
        if (!response.ok) throw new Error('无法创建截图任务');
        const task = await response.json();
        await waitForTask(task.id);
        statusText.textContent = '截图和 AI 分析已更新';
      } catch (error) {
        statusText.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });

    showLatest();
    refreshAnalysis();
    setInterval(refreshAnalysis, 2500);
  </script>
</body>
</html>
""".encode("utf-8")


class ReceiverState:
    def __init__(self, latest_image: Path, latest_answer: Path | None = None,
                 history_dir: Path | None = None, *, profile: str = "default",
                 store: TaskStore | None = None) -> None:
        self.latest_image = Path(latest_image).expanduser()
        self.latest_answer = Path(latest_answer).expanduser() if latest_answer else self.latest_image.with_name("latest.txt")
        self.latest_speech = self.latest_answer.with_name("latest_tts.txt")
        self.latest_state = self.latest_answer.with_name("latest_state.json")
        self.latest_knowledge = self.latest_answer.with_name("latest_knowledge.json")
        self.history_dir = Path(history_dir).expanduser() if history_dir else None
        self.profile = profile
        self.default_prompt = load_default_prompt()
        self.store = store or TaskStore(self.latest_image.with_name("receiver_tasks.sqlite3"))
        self._render_lock = threading.RLock()
        self._condition = threading.Condition()
        self._generations: dict[int, str] = {}
        self._analysis_generation = 0
        self._last_rendered = -1

    def create_task(self) -> str:
        task_id = self.store.create_request(self.profile)
        self.render()
        with self._condition:
            self._condition.notify_all()
        trace("CAPTURE_REQUESTED", task_id)
        return task_id

    def next_task(self, timeout: int) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            task_id = self.store.poll_request(self.profile)
            if task_id:
                return task_id
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with self._condition:
                self._condition.wait(min(remaining, 0.5))

    def task(self, task_id: str) -> dict[str, Any] | None:
        job = self.store.get(task_id)
        if job is None:
            return None
        return {"status": job["state"], "message": job["code"], "created_at": job["created"],
                "attempts": job["attempts"], "analysis_complete": job["state"] == "complete"}

    def fail_task(self, task_id: str, code: str) -> bool:
        result = self.store.fail_request(task_id, code)
        self.render()
        return result

    def storage_ready(self) -> bool:
        try:
            with self.store.tx() as con:
                con.execute("INSERT INTO meta VALUES ('health',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
            # Image, answer, and journal can live in different directories.
            for directory in {self.latest_image.parent, self.latest_answer.parent}:
                private_dir(directory)
                with tempfile.NamedTemporaryFile(dir=directory):
                    pass
            return True
        except (OSError, sqlite3.Error):
            return False

    def save_latest(self, image: bytes) -> None:
        atomic_write(self.latest_image, image)

    def begin_analysis(self, image: bytes | None = None) -> int:
        """Compatibility entry point for embedded callers; main HTTP path uses accept()."""
        task_id = str(uuid.uuid4())
        self._analysis_generation += 1
        generation = self._analysis_generation
        self._generations[generation] = task_id
        self.store.accept(task_id, image or b"\xff\xd8\xff\xd9", origin="embedded",
                          origin_seq=time.time_ns(), profile=self.profile, prompt="", configured=True)
        self.render()
        return generation

    def complete_analysis(self, generation: int, answer: str) -> bool:
        task_id = self._generations[generation]
        job = self.store.claim(model=True, profile=self.profile, task_id=task_id)
        if job is None:
            return False
        applied = self.store.finish(job, answer)
        self.render()
        self.archive(job, answer)
        return applied

    def fail_analysis(self, generation: int, message: str) -> None:
        job = self.store.claim(model=True, profile=self.profile, task_id=self._generations[generation])
        if job:
            self.store.release(job, RetryDecision("pause", "analysis_failed"), model=True)
        self.render()

    def analysis_unconfigured(self) -> None:
        self.render()

    def analysis(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        current = snapshot["current"]
        if current and current["profile"] != self.profile:
            current = None
            snapshot["current"] = None
        stage = current["state"] if current else "idle"
        status = ("processing" if stage in ("queued", "awaiting_capture") else
                  "failed" if stage in ("paused", "uncertain", "expired", "cancelled") else stage)
        return {**snapshot, "status": status, "stage": stage,
                "answer": current["answer"] if current and stage == "complete" else "",
                "message": current["code"] if current else "", "profile": self.profile}

    def render(self) -> None:
        """Project authoritative state to legacy files, always rereading the current ID.

        Old consumers of latest.txt see an explicit current-task header. The canonical
        JSON carries the version; only a real reader ACK can claim DISPLAYED.
        """
        with self._render_lock, FileMutex(self.latest_state.with_suffix(".lock")):
            state = self.analysis()
            if state["version"] == self._last_rendered:
                return
            current, previous = state["current"], state["previous"]
            labels = {"awaiting_capture": "已收到请求，等待截图", "queued": "已接收，等待分析",
                      "processing": "正在分析", "complete": "分析完成", "unconfigured": "模型未配置",
                      "paused": "本次失败，等待处理", "uncertain": "处理结果未知；重试可能再次计费",
                      "expired": "本次任务已过期", "cancelled": "本次任务已取消"}
            text = "当前模式尚无任务。"
            speech = ""
            if current:
                text = f"当前任务：{current['id']}\n{labels.get(current['state'], current['state'])}\n"
                if current["state"] == "complete":
                    text += "\n" + current["answer"]
                    speech = extract_speech_text(current["answer"])
                elif previous and previous["id"] != current["id"] and previous["profile"] == self.profile:
                    text += f"\n上一任务结果（不是本次答案）：{previous['id']}\n{previous['answer']}"
                job = self.store.get(current["id"])
                if job and job["payload"]:
                    atomic_write(self.latest_image, job["payload"])
                else:
                    self.latest_image.unlink(missing_ok=True)
            else:
                self.latest_image.unlink(missing_ok=True)
            atomic_write(self.latest_answer, (text + "\n").encode())
            atomic_write(self.latest_speech, (speech + "\n").encode())
            atomic_json(self.latest_state, state)
            self._last_rendered = state["version"]
            trace("RESULT_STATE_PUBLISHED", current["id"] if current else None, version=state["version"], stage=state["stage"])

    def archive(self, job: dict[str, Any], answer: str) -> None:
        if self.history_dir is None:
            return
        date_dir = datetime.fromtimestamp(job["created"]).strftime("%Y-%m-%d")
        base = self.history_dir / date_dir / job["id"]
        if job["payload"]:
            atomic_write(base.with_suffix(".jpg"), job["payload"])
        atomic_write(base.with_suffix(".txt"), (answer.strip() + "\n").encode())
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        # Only files generated by this implementation are eligible for retention cleanup.
        for folder in self.history_dir.iterdir():
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", folder.name) or folder.name >= cutoff:
                continue
            for path in folder.iterdir():
                if not path.is_symlink() and re.fullmatch(
                    r"[0-9a-f-]{36}\.(jpg|txt|knowledge\.json)", path.name
                ):
                    path.unlink(missing_ok=True)
            if not any(folder.iterdir()):
                folder.rmdir()

    def record_knowledge(
        self,
        job: dict[str, Any],
        result: KnowledgeSearchResult,
    ) -> None:
        payload = {
            "mode": "screenshot",
            "task_id": job["id"],
            "at": time.time(),
            **result.as_dict(),
        }
        current = self.store.snapshot(include_answers=False)["current"]
        if current and current["id"] == job["id"]:
            atomic_json(self.latest_knowledge, payload)
        if self.history_dir is not None:
            date_dir = datetime.fromtimestamp(job["created"]).strftime("%Y-%m-%d")
            atomic_json(
                self.history_dir / date_dir / f"{job['id']}.knowledge.json",
                payload,
            )


class LocalVisionOCR:
    def __init__(
        self,
        executable: Path = DEFAULT_OCR_EXECUTABLE,
        *,
        runner: Any = subprocess.run,
    ) -> None:
        self.executable = Path(executable)
        self.runner = runner

    def extract(self, image: bytes) -> str:
        if not self.executable.is_file():
            raise FileNotFoundError("local OCR helper is missing")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(image)
                stream.flush()
            result = self.runner(
                [str(self.executable), str(temporary)],
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
            )
            if result.returncode != 0:
                raise RuntimeError("local OCR failed")
            return result.stdout.strip()[:24_000]
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class ScreenshotKnowledgeGrounder:
    def __init__(self, service: KnowledgeService, ocr: LocalVisionOCR) -> None:
        self.service = service
        self.ocr = ocr

    def retrieve(self, image: bytes) -> KnowledgeSearchResult:
        if not self.service.enabled():
            return KnowledgeSearchResult.disabled("")
        started = time.monotonic()
        try:
            query = self.ocr.extract(image)
        except FileNotFoundError:
            return KnowledgeSearchResult.failed(
                "",
                "ocr_unavailable",
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return KnowledgeSearchResult.failed(
                "",
                "ocr_failed",
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        if not query:
            return KnowledgeSearchResult(status="empty", query="")
        return self.service.search(query)


class KimiClient:
    def __init__(
        self,
        api_key: str,
        opener: Any = urllib.request.urlopen,
        url: str = BAILIAN_URL,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.api_key = api_key
        self.opener = opener
        self.url = url
        self.model = model

    def analyze(self, image: bytes, prompt: str) -> str:
        image_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "enable_thinking": True,
            "stream": False,
            "max_tokens": 6000,
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
            with self.opener(request, timeout=180) as response:
                body = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            error.close()
            raise  # Preserve status for retry classification; never log the response body.
        try:
            result = json.loads(body)
            answer = result["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError("百炼接口返回了无效结果") from error
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("百炼模型没有返回答案")
        return answer.strip()


class VoicePublisher:
    def __init__(
        self,
        url: str,
        token: str,
        api_key: str,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.url = url.rstrip("/") + "/api/answers"
        self.token = token
        self.api_key = api_key
        self.opener = opener

    def publish(self, answer: str) -> None:
        segments = extract_speech_segments(answer)
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"text": answer, "segments": segments},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Voice-Token": self.token,
                "X-DashScope-Key": self.api_key,
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=15) as response:
                if response.status != HTTPStatus.ACCEPTED:
                    raise RuntimeError(f"voice server returned HTTP {response.status}")
                response.read(64 * 1024)
        except urllib.error.HTTPError as error:
            error.close()
            raise


class AnalysisService:
    def __init__(self, state: ReceiverState, client: KimiClient, prompt: str,
                 voice_publisher: VoicePublisher | None = None,
                 knowledge_grounder: ScreenshotKnowledgeGrounder | None = None) -> None:
        self.state, self.client, self.prompt = state, client, prompt
        self.voice_publisher = voice_publisher
        self.knowledge_grounder = knowledge_grounder

    def submit(self, image: bytes, capture_id: str | None = None) -> None:
        self.state.store.accept(capture_id or str(uuid.uuid4()), image, origin="embedded",
                                origin_seq=time.time_ns(), profile=self.state.profile,
                                prompt=self.prompt, configured=True)
        self.state.render()

    def _render_safely(self, task_id: str) -> None:
        try:
            self.state.render()
        except Exception as error:
            trace("RESULT_RENDER_FAILED", task_id, **error_info(error))

    def run_one(self, heartbeat: Heartbeat) -> bool:
        job = self.state.store.claim(model=True, profile=self.state.profile, lease_seconds=210)
        if job is None:
            return False
        heartbeat.progress("analysis", "requesting_model", deadline=210)
        trace("ANALYSIS_START", job["id"], attempt=job["attempts"])
        self._render_safely(job["id"])
        try:
            effective_prompt = job["prompt"]
            if self.knowledge_grounder is not None:
                try:
                    knowledge = self.knowledge_grounder.retrieve(job["payload"])
                except Exception:
                    knowledge = KnowledgeSearchResult.failed("", "retrieval_exception")
                try:
                    self.state.record_knowledge(job, knowledge)
                except Exception as error:
                    trace("KNOWLEDGE_AUDIT_FAILED", job["id"], **error_info(error))
                trace(
                    "KNOWLEDGE_RETRIEVAL_DONE",
                    job["id"],
                    status=knowledge.status,
                    hit_count=len(knowledge.hits),
                    elapsed_ms=knowledge.elapsed_ms,
                    error_code=knowledge.error_code,
                )
                effective_prompt = ground_text(effective_prompt, knowledge)
            answer = self.client.analyze(job["payload"], effective_prompt)
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty model result")
            applied = self.state.store.finish(job, answer)
            self._render_safely(job["id"])
            trace("ANALYSIS_DONE", job["id"], applied=applied)
        except LeaseLost:
            trace("ANALYSIS_LATE_RESULT_DISCARDED", job["id"])
            return True
        except Exception as error:
            decision = retry_decision(error, job["attempts"], model=True, max_attempts=3)
            self.state.store.release(job, decision, model=True)
            self._render_safely(job["id"])
            trace("ANALYSIS_FAILED", job["id"], code=decision.code, action=decision.action, **error_info(error))
            return True
        finally:
            heartbeat.progress("analysis", "idle")
        # These effects happen AFTER durable result commit. Failure must not resubmit the LLM.
        try:
            self.state.archive(job, answer)
        except Exception as error:
            trace("HISTORY_FAILED", job["id"], **error_info(error))
        current = self.state.store.snapshot(include_answers=False)["current"]
        if applied and self.voice_publisher and current and current["id"] == job["id"]:
            try:
                heartbeat.progress("voice", "publishing", deadline=25)
                self.voice_publisher.publish(extract_speech_text(answer))
            except Exception as error:
                trace("VOICE_PUBLISH_FAILED", job["id"], **error_info(error))
            finally:
                heartbeat.progress("voice", "idle")
        return True


class ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: ReceiverState,
                 analyzer: AnalysisService | None = None, *, node: str = "receiver",
                 tls_context: Any = None, allowed_hosts: tuple[str, ...] = ()) -> None:
        self.state, self.analyzer = state, analyzer
        self.allowed_hosts = set(allowed_hosts)
        self.tls_context = tls_context
        self._http_slots = threading.BoundedSemaphore(16)
        super().__init__(address, ReceiverHandler)
        try:
            self.runtime = ReceiverRuntime(state, analyzer, node=node, on_fault=self.shutdown)
        except BaseException:
            super().server_close()
            raise
        self.stop_event, self.wake, self.fault = self.runtime.stop_event, self.runtime.wake, self.runtime.fault
        self.heartbeat, self.worker, self.monitor = self.runtime.heartbeat, self.runtime.worker, self.runtime.monitor

    def get_request(self):
        connection, address = super().get_request()
        if self.tls_context is not None:
            try:
                connection.settimeout(5)
                connection = self.tls_context.wrap_socket(connection, server_side=True)
            except BaseException:
                connection.close()
                raise
        return connection, address

    def process_request(self, request, client_address):
        if not self._http_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request,client_address)
        except BaseException:
            self._http_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self._http_slots.release()

    @property
    def analyzer(self):
        return self._analyzer

    @analyzer.setter
    def analyzer(self, value):
        self._analyzer = value
        if hasattr(self, "runtime"):
            self.runtime.analyzer = value

    def server_close(self) -> None:
        if hasattr(self, "runtime"):
            self.runtime.close()
        super().server_close()

    def accept_image(self, *args, **kwargs) -> tuple[dict, bool]:
        return self.runtime.accept_image(*args, **kwargs)


class ReceiverHandler(BaseHTTPRequestHandler):
    server: ReceiverServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(35)

    def _authorized(self) -> bool:
        # Protect a loopback service from browser cross-origin requests / DNS rebinding.
        host = urllib.parse.urlsplit("http://" + self.headers.get("Host", "")).hostname
        allowed = {"127.0.0.1", "localhost", "::1", str(self.server.server_address[0])} | self.server.allowed_hosts
        if host not in allowed:
            self._json(HTTPStatus.FORBIDDEN, {"error": "host_not_allowed"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "cross_origin_not_allowed"})
                return False
        token = os.environ.get("LANSHOT_LOCAL_TOKEN", "")
        if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authorization_required"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorized():
            return
        try:
            self._get()
        except (ValueError, TypeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error":"invalid_request"})
        except (OSError, sqlite3.Error) as error:
            trace("HTTP_STORAGE_FAILED", **error_info(error))
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "storage_unavailable"})

    def _get(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, PAGE, "text/html; charset=utf-8")
        elif parsed.path == "/latest.jpg":
            self._send_latest()
        elif parsed.path == "/api/health":
            self._json(HTTPStatus.OK, self.server.runtime.health())
        elif parsed.path.startswith("/api/v1/captures/"):
            task_id = str(uuid.UUID(parsed.path.rsplit("/", 1)[-1]))
            row = self.server.state.store.get(task_id)
            if row is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found", "cluster_id": self.server.runtime.cluster_id})
            else:
                fields = ("id", "origin", "origin_seq", "profile", "created", "updated", "expires", "digest", "state", "answer", "code")
                self._json(HTTPStatus.OK, {"service": "lanshot-receiver", "protocol": PROTOCOL,
                    "cluster_id": self.server.runtime.cluster_id, "durable": True,
                    "task": {k: row[k] for k in fields}})
        elif parsed.path == "/api/status":
            self._json(HTTPStatus.OK, {"service": "lanshot-receiver", "build": BUILD,
                                      **self.server.state.store.snapshot(include_answers=False)})
        elif parsed.path == "/api/analysis":
            self._json(HTTPStatus.OK, self.server.state.analysis())
        elif parsed.path == "/api/v1/agent/next":
            values = urllib.parse.parse_qs(parsed.query)
            if values.get("profile", [self.server.state.profile])[0] != self.server.state.profile:
                self._json(HTTPStatus.CONFLICT, {"error": "profile_mismatch"})
            else:
                self._send_next_task(parsed.query)
        else:
            task_id = self._path_task_id(parsed.path, r"/api/tasks/([^/]+)")
            if task_id:
                self._send_task(task_id)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self.close_connection = True
            return
        try:
            self._post()
        except QueueFull:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "queue_full", "durable": False})
        except Conflict:
            self._json(HTTPStatus.CONFLICT, {"error": "task_conflict", "durable": False})
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
        except (ValueError, TypeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
        except (OSError, sqlite3.Error) as error:
            trace("HTTP_STORAGE_FAILED", **error_info(error))
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "storage_unavailable", "durable": False})

    def _post(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/capture":
            body = self._read_body(MAX_JSON_BYTES)
            if body is None:
                return
            task_id = self.server.state.create_task()
            self._json(HTTPStatus.ACCEPTED, {"id": task_id, "status": "awaiting_capture"})
            return
        task_id = self._path_task_id(path, r"/api/v1/tasks/([^/]+)/image")
        if path == "/api/v1/images" or task_id:
            image = self._read_jpeg()
            if image is None:
                return
            raw = self.headers.get("X-LanShot-Capture-ID") or task_id or str(uuid.uuid4())
            capture_id = str(uuid.UUID(raw))
            if task_id and capture_id != task_id:
                raise Conflict("path and header ID mismatch")
            origin = self.headers.get("X-LanShot-Origin", "legacy")
            sequence = int(self.headers.get("X-LanShot-Sequence", str(time.time_ns())))
            profile = self.headers.get("X-LanShot-Profile", self.server.state.profile)
            created_text = self.headers.get("X-LanShot-Created")
            created = float(created_text) if created_text else None
            if created is not None and not math.isfinite(created):
                raise ValueError("invalid timestamp")
            row, new = self.server.accept_image(capture_id, image, origin=origin, sequence=sequence,
                                                profile=profile, remote=task_id is not None, created=created)
            self._json(HTTPStatus.CREATED if new else HTTPStatus.OK,
                       {"service": "lanshot-receiver", "protocol": PROTOCOL, "capture_id": capture_id,
                        "sha256": row["digest"], "durable": True, "cluster_id": self.server.runtime.cluster_id,
                        "status": "accepted" if new else "duplicate", "state": row["state"]})
            return
        if path in ("/api/retry", "/api/cancel", "/api/displayed"):
            body = self._read_json()
            if body is None:
                return
            task_id = str(uuid.UUID(body["id"]))
            if path == "/api/retry":
                if self.server.analyzer is None:
                    raise Conflict("configure model before retry")
                self.server.state.store.retry(task_id, model=True, confirm_uncertain=body.get("confirm_uncertain") is True)
                self.server.wake.set()
            elif path == "/api/cancel":
                self.server.state.store.cancel(task_id)
            else:
                if not self.server.state.store.mark_displayed(task_id, int(body["version"])):
                    raise Conflict("stale display ACK")
                trace("RESULT_DISPLAYED", task_id, version=int(body["version"]), consumer="diagnostics")
            self.server.state._last_rendered = -1
            self.server.state.render()
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        task_id = self._path_task_id(path, r"/api/v1/tasks/([^/]+)/failure")
        if task_id:
            payload = self._read_json()
            if payload is None:
                return
            code = str(payload.get("code", "capture_failed"))
            if code not in ("capture_failed", "sender_queue_full"):
                code = "capture_failed"
            if not self.server.state.fail_task(task_id, code):
                self._json(HTTPStatus.NOT_FOUND, {"error": "task_not_found_or_already_accepted"})
            else:
                self._json(HTTPStatus.OK, {"status": "paused"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _send_next_task(self, query: str) -> None:
        values = urllib.parse.parse_qs(query)
        try:
            timeout = max(0, min(25, int(values.get("timeout", ["25"])[0])))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_timeout"})
            return
        task_id = self.server.state.next_task(timeout)
        if task_id is None:
            self._send(HTTPStatus.NO_CONTENT, b"", "application/json")
        else:
            self._json(HTTPStatus.OK, {"id": task_id})

    def _send_task(self, task_id: str) -> None:
        task = self.server.state.task(task_id)
        if task is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
        else:
            self._json(HTTPStatus.OK, {"id": task_id, **task})

    def _send_latest(self) -> None:
        path = self.server.state.latest_image
        try:
            image = path.read_bytes()
        except FileNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "no_image"})
            return
        self._send(
            HTTPStatus.OK,
            image,
            "image/jpeg",
            {"Cache-Control": "no-store, max-age=0"},
        )

    def _read_jpeg(self) -> bytes | None:
        if self.headers.get_content_type() != "image/jpeg":
            self.close_connection = True
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "jpeg_required"})
            return None
        body = self._read_body(MAX_IMAGE_BYTES)
        if body is None:
            return None
        if len(body) < 4 or not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_jpeg"})
            return None
        return body

    def _read_json(self) -> dict[str, Any] | None:
        if self.headers.get_content_type() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
            return None
        body = self._read_body(MAX_JSON_BYTES)
        if body is None:
            return None
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return None
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "object_required"})
            return None
        return payload

    def _read_body(self, maximum: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > maximum:
            self.close_connection = True
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid_size"})
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "truncated_body"})
            return None
        return body

    @staticmethod
    def _path_task_id(path: str, pattern: str) -> str | None:
        match = re.fullmatch(pattern, path)
        if not match:
            return None
        try:
            return str(uuid.UUID(match.group(1)))
        except ValueError:
            return None

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return  # Do not copy URLs, tokens, or user payloads into access logs.


def create_server(
    host: str,
    port: int,
    state: ReceiverState,
    analyzer: AnalysisService | None = None,
    *, node: str = "receiver", tls_context: Any = None, allowed_hosts: tuple[str, ...] = (),
) -> ReceiverServer:
    return ReceiverServer((host, port), state, analyzer, node=node, tls_context=tls_context, allowed_hosts=allowed_hosts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LanShot receiver service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--node", default="receiver")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--public-host", action="append", default=[])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE_PATH)
    parser.add_argument("--answer-file", type=Path, default=DEFAULT_ANSWER_PATH)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--prompt", default=load_default_prompt())
    parser.add_argument("--voice-url", default=os.environ.get("LANSHOT_VOICE_URL", ""))
    parser.add_argument("--voice-token", default=os.environ.get("LANSHOT_VOICE_TOKEN", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if arguments.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("LANSHOT_LOCAL_TOKEN"):
        raise ValueError("non-loopback binding requires LANSHOT_LOCAL_TOKEN")
    context = None
    if bool(arguments.tls_cert) != bool(arguments.tls_key):
        raise ValueError("both TLS certificate and key are required")
    if arguments.tls_cert:
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(arguments.tls_cert, arguments.tls_key)
    if arguments.host not in ("127.0.0.1", "localhost", "::1") and context is None:
        raise ValueError("non-loopback binding requires TLS; use loopback behind a TLS reverse proxy")
    state = ReceiverState(arguments.image, arguments.answer_file, arguments.history_dir, profile=arguments.profile)
    state.default_prompt = arguments.prompt
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    voice_publisher = None
    if arguments.voice_url and arguments.voice_token and api_key:
        voice_publisher = VoicePublisher(
            arguments.voice_url,
            arguments.voice_token,
            api_key,
        )
    analyzer = None
    if api_key:
        knowledge_service = KnowledgeService(lambda: api_key)
        knowledge_grounder = ScreenshotKnowledgeGrounder(
            knowledge_service,
            LocalVisionOCR(),
        )
        analyzer = AnalysisService(
            state,
            KimiClient(api_key),
            arguments.prompt,
            voice_publisher,
            knowledge_grounder,
        )
    server = create_server(arguments.host, arguments.port, state, analyzer, node=arguments.node, tls_context=context, allowed_hosts=tuple(arguments.public_host))
    LOGGER.info(
        "receiver listening on http://%s:%d; history=%s",
        arguments.host,
        arguments.port,
        arguments.history_dir,
    )
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 75 if server.fault.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
