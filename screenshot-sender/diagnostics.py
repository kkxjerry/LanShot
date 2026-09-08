#!/usr/bin/env python3
"""Command-line diagnostics and manual capture; the native app is the normal display."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
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
                     "error_type", "errno", "http_status", "attempt", "delay", "elapsed_ms", "version", "backend", "node", "cluster_id"}
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
        if self.config.routes_file:
            from multi_receiver import build_client
            try:
                router = build_client(self.config,start_embedded=False)
                result["receiver"] = router.local_state.store.snapshot(include_answers=False) if router.local_state else {"status":"unavailable"}
                result["routes"] = router.book.summary()
                result["receiver_nodes"] = {name:self._read_status(Path(self.data["receiver_dir"])/f"{name}_status.json")
                                            for name in ("embedded","receiver","receiver_backup")}
                result["display"] = self._read_status(Path(self.data["display_dir"])/"display_status.json")
                result["overlay"] = self._read_status(Path(self.data["display_dir"])/"overlay_status.json")
                router.close()
            except Exception as error:
                result["receiver"] = {"status":"unavailable",**error_info(error)}
            return result
        try:
            result["receiver"] = self.request("/api/status")
            result["receiver_health"] = self.request("/api/health")
        except Exception as error:
            result["receiver"] = {"status": "unavailable", **error_info(error)}
        return result

    def capture(self) -> str:
        if self.config.routes_file:
            from multi_receiver import build_client
            router = build_client(self.config,start_embedded=False)
            try:
                if router.local_state is None:
                    raise ValueError("local manual-request store unavailable")
                return router.local_state.create_task()
            finally:
                router.close()
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
        safe["routes"] = [{k: v for k, v in row.items() if k in ("id", "backend", "cluster_id", "state", "updated")}
                          for row in status.get("routes", [])[:100] if isinstance(row, dict)]
        for key in ("display", "overlay"):
            value = status.get(key, {})
            safe[key] = {k: value.get(k) for k in ("status", "age_seconds", "heartbeat_fresh")}
        safe["receiver_nodes"] = {name: {k: value.get(k) for k in ("status", "age_seconds", "heartbeat_fresh")}
            for name, value in status.get("receiver_nodes", {}).items()
            if name in ("embedded", "receiver", "receiver_backup") and isinstance(value, dict)}
        events = []
        root = Path(self.data["state_dir"])
        for role in ("sender", "receiver", "receiver_backup", "display"):
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "capture", "capture-test", "queue", "routes"):
        sub.add_parser(name)
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    for name in ("retry-upload", "cancel-upload"):
        command = sub.add_parser(name)
        command.add_argument("--id", required=True)
        command.add_argument("--confirm-uncertain",action="store_true")
    for name in ("retry-analysis","cancel-analysis"):
        command = sub.add_parser(name)
        command.add_argument("--id",required=True)
        command.add_argument("--confirm-uncertain",action="store_true")
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
        if diag.config.routes_file and sys.platform == "darwin":
            from multi_receiver import load_routes
            for endpoint in load_routes(diag.config.routes_file,diag.config.profile)["parsed_endpoints"]:
                if endpoint.kind == "remote":
                    value = keychain(endpoint.token_env,service="com.lanshot.p1")
                    if value:
                        os.environ[endpoint.token_env]=value
        if args.command == "status":
            result = diag.status()
        elif args.command == "capture":
            result = {"task_id": diag.capture(), "status": "requested_not_completed"}
        elif args.command == "capture-test":
            result = diag.local_capture_test()
        elif args.command == "export":
            result = {"archive": str(diag.export(args.output))}
        elif args.command == "routes":
            from multi_receiver import RouteBook
            if diag.config.routes_file:
                from multi_receiver import build_client
                router = build_client(diag.config,start_embedded=False)
                try:
                    result = {"queue_target":router.queue_target,"tasks":router.book.summary()}
                finally:
                    router.close()
            else:
                result = RouteBook(diag.sender_store()).summary()
        elif args.command == "queue":
            result = diag.sender_store().snapshot(include_answers=False)
        elif args.command in ("retry-upload", "cancel-upload"):
            store = diag.sender_store()
            task_id = str(uuid.UUID(args.id))
            store.retry(task_id,confirm_uncertain=args.confirm_uncertain) if args.command == "retry-upload" else store.cancel(task_id)
            result = {"status": "updated", "task_id": task_id}
        elif args.command in ("retry-analysis","cancel-analysis"):
            if not diag.config.routes_file:
                result = diag.request("/api/retry" if args.command == "retry-analysis" else "/api/cancel",
                    {"id":args.id,"confirm_uncertain":args.confirm_uncertain})
            else:
                from multi_receiver import build_client
                router = build_client(diag.config,start_embedded=False)
                try:
                    result = router.control_task(args.id,"retry" if args.command == "retry-analysis" else "cancel",
                                                 confirm_uncertain=args.confirm_uncertain)
                finally:
                    router.close()
        else:
            if diag.config.routes_file:
                raise ValueError("P0 migration requires an explicit single-target migration configuration; never import old ambiguous work into multi-route failover")
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
