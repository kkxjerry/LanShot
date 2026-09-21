"""Bounded OpenAI-compatible SSE reader; expose answer content, never reasoning."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def ask_stream(client, question: str, prompt: str, history=None, *, on_update=None) -> str:
    started = time.monotonic()
    client.last_timing = {}
    client.last_usage = {}
    messages = [{"role": "system", "content": prompt}]
    for item in history or []:
        messages.extend([
            {"role": "user", "content": item["input"]},
            {"role": "assistant", "content": item["answer"]},
        ])
    messages.append({"role": "user", "content": question})
    enable_thinking = bool(getattr(client, "enable_thinking", True))
    reasoning_effort = str(getattr(client, "reasoning_effort", "low"))
    payload = {
        "model": client.model, "messages": messages,
        "enable_thinking": enable_thinking, "reasoning_effort": reasoning_effort,
        "stream": True, "stream_options": {"include_usage": True},
        "max_tokens": 2400,
    }
    request = urllib.request.Request(
        client.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + client.api_key,
                 "Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    parts = []
    first_visible = None
    last_publish = 0.0
    published = ""
    finish_reason = None
    total_bytes = 0
    event_count = 0
    done = False

    def consume(data: str) -> None:
        nonlocal first_visible, last_publish, published, finish_reason, event_count, done
        if data.strip() == "[DONE]":
            done = True
            return
        try:
            value = json.loads(data)
        except (ValueError, TypeError) as error:
            raise RuntimeError("百炼返回了无效的流式数据") from error
        if not isinstance(value, dict) or value.get("error"):
            raise RuntimeError("百炼流式请求失败")
        event_count += 1
        if isinstance(value.get("usage"), dict):
            client.last_usage = value["usage"]
        choices = value.get("choices") or []
        if not choices:
            return
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise RuntimeError("百炼返回了无效的流式结果")
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise RuntimeError("百炼返回了无效的流式增量")
        content = delta.get("content")
        # reasoning_content is deliberately neither displayed nor persisted.
        if isinstance(content, str) and content:
            now = time.monotonic()
            parts.append(content)
            if first_visible is None and "".join(parts).strip():
                first_visible = round((now - started) * 1000)
            if on_update is not None and (not published or now - last_publish >= 0.08):
                partial = "".join(parts).strip()
                if partial and partial != published:
                    on_update(partial)
                    published, last_publish = partial, now

    try:
        with client.opener(request, timeout=30) as response:
            event_lines = []
            for raw in response:
                total_bytes += len(raw)
                if total_bytes > 2 * 1024 * 1024:
                    raise RuntimeError("百炼流式响应超过大小限制")
                if time.monotonic() - started > 45:
                    raise RuntimeError("百炼生成超过等待预算")
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if event_lines:
                        consume("\n".join(event_lines))
                        event_lines = []
                    if done:
                        break
                elif line.startswith("data:"):
                    event_lines.append(line[5:].lstrip())
            if event_lines and not done:
                consume("\n".join(event_lines))
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError(f"百炼请求失败，HTTP {status}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError("百炼连接中断或超时") from error
    except UnicodeDecodeError as error:
        raise RuntimeError("百炼返回了无效的流式编码") from error
    answer = "".join(parts).strip()
    client.last_timing = {
        "provider": "bailian_compatible", "streaming": True,
        "thinking": enable_thinking,
        "thinking_level": reasoning_effort if enable_thinking else "none",
        "first_visible_ms": first_visible,
        "complete_ms": round((time.monotonic() - started) * 1000),
        "finish_reason": finish_reason, "event_count": event_count,
        "output_chars": len(answer),
    }
    if finish_reason == "length":
        raise RuntimeError("答案达到模型输出上限，不能当作完整答案")
    if finish_reason not in ("stop",) or not answer:
        raise RuntimeError("百炼未返回完整答案")
    if on_update is not None and answer != published:
        on_update(answer)
    return answer
