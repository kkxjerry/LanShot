#!/usr/bin/env python3
"""Visible local diagnostics and manual capture. No stealth window or browser automation."""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

from reliability import BUILD, TaskStore, error_info
from manage_services import DEFAULT_SETTINGS, read_settings
from sender_service import Config, Screenshotter, NativeRedactor

SAFE_EVENT_FIELDS = {"event", "at", "task_id", "build", "stage", "component", "code",
                     "error_type", "errno", "http_status", "attempt", "delay", "elapsed_ms", "version"}
SAFE_JOB_FIELDS = {"id", "state", "created", "updated", "attempts", "code", "profile", "displayed_version"}


class Diagnostics:
    def __init__(self, settings: Path):
        self.settings = settings
        self.data = read_settings(settings)
        self.config = Config.load(Path(self.data["sender_config"]))

    def request(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("LANSHOT_LOCAL_TOKEN", "")
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.config.server_url + endpoint, headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=3) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("diagnostic response too large")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("invalid diagnostic response")
        return value

    @staticmethod
    def _read_status(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text())
            age = time.time() - float(value["at"])
            return {"status": value.get("status", "unknown"), "age_seconds": round(age, 1),
                    "heartbeat_fresh": -2 <= age <= 5, "build": value.get("build"),
                    "input_error": value.get("input_error"), "components": value.get("components", {}),
                    "last_capture": value.get("last_capture", {}), "queue": value.get("queue", {}),
                    "receiver": value.get("receiver", "unknown")}
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "missing_or_invalid", "heartbeat_fresh": False}

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"build": BUILD, "profile": self.config.profile,
                                  "sender": self._read_status(self.config.spool_dir / "sender_status.json")}
        try:
            result["receiver"] = self.request("/api/status")
            result["receiver_health"] = self.request("/api/health")
        except Exception as error:
            result["receiver"] = {"status": "unavailable", **error_info(error)}
        return result

    def view(self) -> dict[str, Any]:
        sender = self._read_status(self.config.spool_dir / "sender_status.json")
        try:
            result = self.request("/api/analysis")
        except Exception:
            result = {"status": "unavailable", "stage": "receiver_unavailable", "current": None,
                      "previous": None, "version": 0, "profile": self.config.profile}
        result["sender"] = sender
        return result

    def capture(self) -> str:
        return str(self.request("/api/capture", {})["id"])

    def local_capture_test(self) -> dict[str, str]:
        # This path has NO HTTP client. It cannot upload a screenshot.
        with tempfile.TemporaryDirectory(prefix="lanshot-local-diagnostic-") as directory:
            shot = Screenshotter(Path(directory), self.config.include_cursor,
                                redactor=NativeRedactor(self.config.blocked_words_file)).capture(uuid.uuid4())
            if not shot.is_file() or shot.stat().st_size == 0:
                raise ValueError("empty capture")
        return {"capture": "local_capture_passed", "uploaded": "no", "content_correctness": "requires_visual_check"}

    def export(self, output: Path) -> Path:
        status = self.status()
        receiver = status.get("receiver", {})
        # Defense in depth: ignore arbitrary server payloads and raw status/log dictionaries.
        safe = {"build": BUILD, "platform": platform.system(), "platform_release": platform.release(),
                "python": platform.python_version(), "profile": self.config.profile,
                "queue_limits": {"items": self.config.queue_max_items, "bytes": self.config.queue_max_bytes,
                                 "ttl_seconds": self.config.queue_ttl_seconds, "max_attempts": self.config.upload_max_attempts},
                "sender": {k: status["sender"].get(k) for k in ("status", "age_seconds", "heartbeat_fresh", "input_error")},
                "receiver": {k: receiver.get(k) for k in ("version", "counts", "queued_bytes", "status")}}
        for key in ("current", "previous"):
            row = receiver.get(key)
            safe["receiver"][key] = {k: v for k, v in row.items() if k in SAFE_JOB_FIELDS} if isinstance(row, dict) else None
        events = []
        root = Path(self.data["state_dir"])
        for role in ("sender", "receiver"):
            path = root / f"{role}.log"
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 256 * 1024))
                lines = stream.read(256 * 1024).decode("utf-8", "replace").splitlines()
            for line in lines[-500:]:
                try:
                    event = json.loads(line.split("trace=", 1)[1])
                    events.append({k: v for k, v in event.items() if k in SAFE_EVENT_FIELDS})
                except (ValueError, IndexError, AttributeError):
                    continue
        output = output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Never overwrite an existing archive silently.
        with output.open("xb") as raw:
            os.chmod(output, 0o600)
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("status.json", json.dumps(safe, ensure_ascii=False, indent=2))
                archive.writestr("events.jsonl", "\n".join(json.dumps(x, ensure_ascii=False) for x in events[-300:]))
                archive.writestr("PRIVACY.txt", "Whitelist-only metadata. No screenshots, answers, prompts, raw logs, settings files, API keys or environment variables.\n")
        return output

    def sender_store(self) -> TaskStore:
        return TaskStore(self.config.spool_dir / "sender_tasks.sqlite3", ttl=self.config.queue_ttl_seconds,
                         max_items=self.config.queue_max_items, max_bytes=self.config.queue_max_bytes)


def gui(diag: Diagnostics) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, filedialog
    except ImportError as error:
        raise RuntimeError("Tk is unavailable in this Python. CLI status/export/capture remain usable.") from error
    root = tk.Tk()
    root.title("LanShot P1 · 本地状态与手动操作")
    root.geometry("780x620")
    status = tk.StringVar(value="连接本地服务…")
    tk.Label(root, textvariable=status, anchor="w", justify="left", wraplength=750).pack(fill="x", padx=12, pady=10)
    buttons = tk.Frame(root)
    buttons.pack(fill="x", padx=12)
    body = tk.Text(root, wrap="word", font=("TkDefaultFont", 13))
    body.pack(fill="both", expand=True, padx=12, pady=12)
    results: queue.Queue[tuple[str, Any]] = queue.Queue()
    current: dict[str, Any] = {}
    busy = {"poll": False}
    def work(name, function):
        def run():
            try:
                results.put((name, function()))
            except Exception as error:
                results.put(("error:" + name, error_info(error)))
        threading.Thread(target=run, daemon=True).start()
    def capture():
        status.set("请求截图：此操作会按配置上传到接收端并调用模型。")
        work("capture", diag.capture)
    def retry():
        row = current.get("current") or {}
        if not row:
            return
        uncertain = row.get("state") == "uncertain"
        if uncertain and not messagebox.askyesno("处理结果未知", f"任务 {row['id']} 可能已经调用过模型。再次处理可能重复计费，仍要重试吗？"):
            return
        work("retry", lambda: diag.request("/api/retry", {"id": row["id"], "confirm_uncertain": uncertain}))
    def cancel():
        row = current.get("current") or {}
        if row:
            work("cancel", lambda: diag.request("/api/cancel", {"id": row["id"]}))
    def export():
        name = filedialog.asksaveasfilename(defaultextension=".zip", initialfile="lanshot-diagnostics.zip")
        if name:
            work("export", lambda: str(diag.export(Path(name))))
    tk.Button(buttons, text="手动截图并分析", command=capture).pack(side="left", padx=4)
    tk.Button(buttons, text="本地截图自检（不上传）", command=lambda: work("capture-test", diag.local_capture_test)).pack(side="left", padx=4)
    tk.Button(buttons, text="重试当前任务", command=retry).pack(side="left", padx=4)
    tk.Button(buttons, text="取消等待任务", command=cancel).pack(side="left", padx=4)
    tk.Button(buttons, text="导出诊断", command=export).pack(side="left", padx=4)
    rendered: tuple[str, int] | None = None
    def tick():
        nonlocal rendered
        while True:
            try:
                name, value = results.get_nowait()
            except queue.Empty:
                break
            if name in ("view", "error:view"):
                busy["poll"] = False
            if name.startswith("error:"):
                status.set(f"{name[6:]} 操作失败：{value.get('error_type')}。未确认当前服务状态；下方可能是历史缓存。")
                continue
            if name != "view":
                status.set(f"{name}：{value}")
                continue
            current.clear()
            current.update(value)
            row, previous = value.get("current"), value.get("previous")
            text = "当前模式尚无任务。"
            if row:
                text = f"当前任务：{row['id']}\n阶段：{row['state']}\n错误码：{row.get('code') or '无'}\n\n"
                if row["state"] == "complete":
                    text += row["answer"]
                elif previous and previous["id"] != row["id"] and previous["profile"] == value.get("profile"):
                    text += f"上一任务结果（不是本次答案）：{previous['id']}\n{previous['answer']}"
            sender_state = value.get("sender", {})
            sender_queue = sender_state.get("queue", {}).get("counts", {})
            status.set(f"模式 {value.get('profile')} · 阶段 {value.get('stage')} · 版本 {value.get('version')}\n"
                       f"发送端：{sender_state.get('status')} · 上传队列：{sender_queue} · 最近截图：{sender_state.get('last_capture', {})}")
            body.delete("1.0", "end")
            body.insert("1.0", text)
            if row and row["state"] == "complete":
                version = int(value["version"])
                if rendered != (row["id"], version):
                    rendered = (row["id"], version)
                    root.after_idle(lambda task_id=row["id"], v=version: work("displayed", lambda: diag.request("/api/displayed", {"id": task_id, "version": v})))
        if not busy["poll"]:
            busy["poll"] = True
            work("view", diag.view)
        root.after(1000, tick)
    root.after(0, tick)
    try:
        root.mainloop()
    finally:
        # Tcl timers survive destruction of a toplevel. Cancel them explicitly and
        # release Tk-owned objects on the GUI thread, not a later worker-thread GC.
        try:
            for after_id in root.tk.splitlist(root.tk.call("after", "info")):
                root.tk.call("after", "cancel", after_id)
        except tk.TclError:
            pass
        status = None
        body = None
        buttons = None
        root = None
        import gc
        gc.collect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "gui", "capture", "capture-test", "queue"):
        sub.add_parser(name)
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    for name in ("retry-upload", "cancel-upload"):
        command = sub.add_parser(name)
        command.add_argument("--id", required=True)
    migration = sub.add_parser("migrate-p0-sender")
    migration.add_argument("--spool", type=Path, required=True)
    migration.add_argument("--confirm-target", required=True)
    args = parser.parse_args(argv)
    try:
        if sys.platform == "darwin":
            from manage_services import keychain
            token = keychain("LANSHOT_LOCAL_TOKEN", service="com.lanshot.p1")
            if token:
                os.environ["LANSHOT_LOCAL_TOKEN"] = token
        diag = Diagnostics(args.settings)
        if args.command == "gui":
            gui(diag)
            return 0
        if args.command == "status":
            result = diag.status()
        elif args.command == "capture":
            result = {"task_id": diag.capture(), "status": "requested_not_completed"}
        elif args.command == "capture-test":
            result = diag.local_capture_test()
        elif args.command == "export":
            result = {"archive": str(diag.export(args.output))}
        elif args.command == "queue":
            result = diag.sender_store().snapshot(include_answers=False)
        elif args.command in ("retry-upload", "cancel-upload"):
            store = diag.sender_store()
            task_id = str(uuid.UUID(args.id))
            store.retry(task_id) if args.command == "retry-upload" else store.cancel(task_id)
            result = {"status": "updated", "task_id": task_id}
        else:
            if args.confirm_target != diag.config.server_url:
                raise ValueError("confirmed target does not match this sender configuration")
            result = diag.sender_store().migrate_pending(args.spool, target=diag.config.server_url, profile=diag.config.profile)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", **error_info(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
