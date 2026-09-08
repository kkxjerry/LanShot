#!/usr/bin/env python3
"""LanShot sender: capture the main display and upload it to a LAN server."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
import sqlite3
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping


from reliability import (BUILD, PROTOCOL, TaskStore, QueueFull, Conflict, InstanceLock,
                         Heartbeat, RetryDecision, LeaseLost, retry_decision, error_info,
                         trace, atomic_json, private_dir)

LOGGER = logging.getLogger("lanshot-sender")
LAUNCH_AGENT_LABEL = "com.lanshot.sender"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "lanshot-sender" / "config.json"
DEFAULT_SPOOL_DIR = Path.home() / "Library" / "Caches" / "LanShotSender" / "captures"
DEFAULT_BLOCKED_WORDS_PATH = Path.home() / ".config" / "lanshot-sender" / "blocked_words.txt"
DEFAULT_PAGE_COMMAND_PATH = Path.home() / ".local" / "share" / "lanshot-receiver" / "page.txt"
DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "LanShotSender.log"
DEFAULT_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
SCREEN_CAPTURE_EXECUTABLE = Path("/usr/sbin/screencapture")
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
F24_FUNCTION_KEY = "\uf71b"
MOUSE_F24_KEYCODE = 65287
F23_FUNCTION_KEY = "\uf71a"
MOUSE_F23_KEYCODE = 65286
F22_FUNCTION_KEY = "\uf719"
MOUSE_F22_KEYCODE = 65285


class ConfigError(ValueError):
    pass


class CaptureError(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    server_url: str
    interval_seconds: float = 0
    poll_timeout_seconds: int = 25
    request_timeout_seconds: float = 35
    include_cursor: bool = False
    retry_delays: tuple[float, ...] = field(default=(0.5, 1.0, 2.0))
    spool_dir: Path = DEFAULT_SPOOL_DIR
    blocked_words_file: Path = DEFAULT_BLOCKED_WORDS_PATH
    profile: str = "default"
    queue_ttl_seconds: float = 900
    queue_max_items: int = 100
    queue_max_bytes: int = 256 * 1024 * 1024
    upload_max_attempts: int = 6
    routes_file: Path | None = None
    display_dir: Path | None = None

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError("server_url must be an http:// or https:// URL")
        if parsed.query or parsed.fragment:
            raise ConfigError("server_url cannot contain a query or fragment")
        if self.interval_seconds < 0:
            raise ConfigError("interval_seconds must be zero or greater")
        if not 1 <= self.poll_timeout_seconds <= 25:
            raise ConfigError("poll_timeout_seconds must be between 1 and 25")
        if self.request_timeout_seconds <= 0:
            raise ConfigError("request_timeout_seconds must be greater than zero")
        if any(delay < 0 for delay in self.retry_delays):
            raise ConfigError("retry_delays cannot contain negative values")

        if parsed.username or parsed.password:
            raise ConfigError("credentials in server_url are not supported; use LANSHOT_LOCAL_TOKEN")
        if not self.profile or len(self.profile) > 64 or not self.profile.replace("-", "").replace("_", "").isalnum():
            raise ConfigError("invalid profile")
        if self.queue_max_items < 1 or self.queue_max_bytes < 1 or self.upload_max_attempts < 1:
            raise ConfigError("queue limits must be positive")
        if not math.isfinite(self.queue_ttl_seconds) or self.queue_ttl_seconds <= 0:
            raise ConfigError("queue TTL must be positive and finite")
        if not math.isfinite(self.request_timeout_seconds):
            raise ConfigError("request timeout must be finite")
        normalized_path = parsed.path.rstrip("/")
        normalized = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, normalized_path, "", "")
        )
        object.__setattr__(self, "server_url", normalized)
        object.__setattr__(self, "retry_delays", tuple(self.retry_delays))
        object.__setattr__(self, "spool_dir", Path(self.spool_dir).expanduser())
        for name in ("routes_file", "display_dir"):
            if getattr(self,name) is not None:
                object.__setattr__(self,name,Path(getattr(self,name)).expanduser())
        object.__setattr__(
            self, "blocked_words_file", Path(self.blocked_words_file).expanduser()
        )

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ConfigError(f"config file not found: {path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"cannot read config file: {path}") from error
        if not isinstance(raw, dict):
            raise ConfigError("config must contain a JSON object")
        if "retry_delays" in raw:
            raw["retry_delays"] = tuple(raw["retry_delays"])
        if "spool_dir" in raw:
            raw["spool_dir"] = Path(raw["spool_dir"])
        if "blocked_words_file" in raw:
            raw["blocked_words_file"] = Path(raw["blocked_words_file"])
        try:
            return cls(**raw)
        except (TypeError, ValueError) as error:
            raise ConfigError(str(error)) from error

    def write(self, path: Path) -> None:
        output = asdict(self)
        output["retry_delays"] = list(self.retry_delays)
        output["spool_dir"] = str(self.spool_dir)
        output["blocked_words_file"] = str(self.blocked_words_file)
        for name in ("routes_file", "display_dir"):
            output[name] = str(getattr(self,name)) if getattr(self,name) is not None else None
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        path.chmod(0o600)


class Screenshotter:
    def __init__(
        self,
        spool_dir: Path,
        include_cursor: bool = False,
        executable: Path = SCREEN_CAPTURE_EXECUTABLE,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        redactor: "NativeRedactor | None" = None,
    ) -> None:
        self.spool_dir = Path(spool_dir).expanduser()
        self.include_cursor = include_cursor
        self.executable = executable
        self.runner = runner
        self.redactor = redactor

    def capture(self, capture_id: uuid.UUID) -> Path:
        trace("CAPTURE_START", capture_id)
        started = time.monotonic()
        private_dir(self.spool_dir)
        output = self.spool_dir / f"{capture_id}.jpg"
        output.unlink(missing_ok=True)
        command = [str(self.executable), "-x", "-m", "-t", "jpg"]
        if self.include_cursor:
            command.append("-C")
        command.append(str(output))

        try:
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            output.unlink(missing_ok=True)
            raise CaptureError("could not run macOS screencapture") from error

        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            trace("CAPTURE_BACKEND_FAILED", capture_id, returncode=result.returncode,
                  stderr_class="permission" if "permission" in (result.stderr or "").lower() else "backend_error",
                  elapsed_ms=round((time.monotonic() - started) * 1000))
            output.unlink(missing_ok=True)
            raise CaptureError(
                "screen capture failed; grant Screen Recording permission to Python/Terminal"
            )
        if output.stat().st_size > MAX_IMAGE_BYTES:
            output.unlink(missing_ok=True)
            raise CaptureError("captured image exceeds 25 MB")
        with output.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                output.unlink(missing_ok=True)
                raise CaptureError("screencapture did not produce a JPEG image")
        if self.redactor is not None:
            try:
                self.redactor.redact(output)
            except Exception:
                output.unlink(missing_ok=True)
                raise
        output.chmod(0o600)
        trace("CAPTURE_OK", capture_id, bytes=output.stat().st_size,
              elapsed_ms=round((time.monotonic() - started) * 1000))
        return output


class NativeRedactor:
    def __init__(
        self,
        blocked_words_file: Path,
        executable: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.blocked_words_file = Path(blocked_words_file).expanduser()
        self.executable = executable or Path(__file__).with_name("native_redactor")
        self.runner = runner

    def redact(self, image_path: Path) -> None:
        words = self._load_words()
        if not words:
            return
        if not self.executable.is_file():
            raise CaptureError(f"OCR redactor not found: {self.executable}")

        temporary = image_path.with_name(image_path.stem + ".redacted.jpg")
        temporary.unlink(missing_ok=True)
        try:
            result = self.runner(
                [str(self.executable), str(image_path), str(temporary), *words],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if result.returncode != 0 or not temporary.is_file():
                message = result.stderr.strip() or "local OCR redaction failed"
                raise CaptureError(message[:300])
            if not temporary.read_bytes().startswith(b"\xff\xd8"):
                raise CaptureError("OCR redactor did not produce a JPEG image")
            os.replace(temporary, image_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_words(self) -> list[str]:
        try:
            lines = self.blocked_words_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise CaptureError("cannot read blocked words file") from error
        return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


class HTTPClient:
    def __init__(
        self,
        config: Config,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.opener = opener
        self.sleep = sleep

    def poll_task(self) -> uuid.UUID | None:
        timeout = self.config.poll_timeout_seconds
        status, body = self._request(
            "GET",
            f"/api/v1/agent/next?timeout={timeout}&profile={urllib.parse.quote(self.config.profile)}",
            timeout=max(self.config.request_timeout_seconds, timeout + 5),
        )
        if status == 204:
            return None
        payload = self._decode_json(body)
        task_id = payload.get("id")
        try:
            return uuid.UUID(str(task_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ProtocolError("server returned an invalid task id") from error

    def upload_remote(self, task_id: uuid.UUID, image_path: Path) -> None:
        image = self._read_jpeg(image_path)
        self._request(
            "POST",
            f"/api/v1/tasks/{task_id}/image",
            data=image,
            headers={"Content-Type": "image/jpeg"},
        )

    def upload_scheduled(self, capture_id: uuid.UUID, image_path: Path) -> None:
        image = self._read_jpeg(image_path)
        self._request(
            "POST",
            "/api/v1/images",
            data=image,
            headers={
                "Content-Type": "image/jpeg",
                "X-LanShot-Capture-ID": str(capture_id),
            },
        )

    def health(self) -> dict[str, Any]:
        status, body = self._request("GET", "/api/health", timeout=3, attempts=1)
        payload = self._decode_json(body)
        if status != 200 or payload.get("service") != "lanshot-receiver" or payload.get("protocol") != PROTOCOL:
            raise ProtocolError("wrong receiver identity or incompatible protocol")
        if (payload.get("profile") != self.config.profile or payload.get("storage") != "ready"
                or payload.get("worker") != "running" or payload.get("status") != "ready"):
            raise ProtocolError("receiver profile mismatch or storage unavailable")
        return payload

    def upload_record(self, job: dict[str, Any]) -> dict[str, Any]:
        self.health()  # Verify protocol/profile BEFORE a potentially billable POST.
        path = f"/api/v1/tasks/{job['id']}/image" if job["kind"] == "remote" else "/api/v1/images"
        status, body = self._request("POST", path, data=job["payload"], headers={
            "Content-Type": "image/jpeg", "X-LanShot-Capture-ID": job["id"],
            "X-LanShot-Origin": job["origin"], "X-LanShot-Sequence": str(job["origin_seq"]),
            "X-LanShot-Profile": job["profile"], "X-LanShot-Created": str(job["created"]),
        }, attempts=1)
        ack = self._decode_json(body)
        if (status not in (200, 201, 202) or ack.get("service") != "lanshot-receiver"
                or ack.get("protocol") != PROTOCOL or ack.get("capture_id") != job["id"]
                or ack.get("sha256") != job["digest"] or ack.get("durable") is not True):
            raise ProtocolError("receiver did not provide a matching durable ACK; keeping payload")
        return ack

    def report_failure(self, task_id: uuid.UUID, code: str, message: str) -> None:
        body = json.dumps({"code": code, "message": message}).encode("utf-8")
        self._request(
            "POST",
            f"/api/v1/tasks/{task_id}/failure",
            data=body,
            headers={"Content-Type": "application/json"},
        )

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> tuple[int, bytes]:
        url = f"{self.config.server_url}{path}"
        request_headers = {"User-Agent": "LanShotSender/1.0"}
        token = os.environ.get("LANSHOT_LOCAL_TOKEN", "")
        if token:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        timeout = timeout or self.config.request_timeout_seconds

        total_attempts = attempts if attempts is not None else (1 if method == "POST" else len(self.config.retry_delays) + 1)
        for attempt in range(total_attempts):
            request = urllib.request.Request(
                url,
                data=data,
                headers=request_headers,
                method=method,
            )
            try:
                with self.opener(request, timeout=timeout) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ProtocolError("server response is too large")
                    return int(response.status), body
            except urllib.error.HTTPError as error:
                trace("HTTP_FAILED", (headers or {}).get("X-LanShot-Capture-ID"), attempt=attempt + 1, **error_info(error))
                error.close()
                if error.code < 500 or attempt + 1 >= total_attempts:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                trace("HTTP_FAILED", (headers or {}).get("X-LanShot-Capture-ID"), attempt=attempt + 1, **error_info(error))
                if attempt + 1 >= total_attempts:
                    raise
            self.sleep(self.config.retry_delays[min(attempt, len(self.config.retry_delays) - 1)])

        raise RuntimeError("unreachable retry state")

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError("server returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ProtocolError("server returned a non-object JSON response")
        return payload

    @staticmethod
    def _read_jpeg(path: Path) -> bytes:
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise CaptureError("captured image exceeds 25 MB")
        data = path.read_bytes()
        if not data.startswith(b"\xff\xd8"):
            raise CaptureError("upload file is not JPEG")
        return data


class MacF24Listener:
    """Listen for the global macOS F22-F24 keys without dependencies."""

    KEY_DOWN_EVENT = 10
    COMMAND_FLAG_MASK = 1 << 20
    AUTOREPEAT_FIELD = 8
    KEYCODE_FIELD = 9

    def __init__(self) -> None:
        self._run_loop: int | None = None
        self._core_foundation: Any = None
        self._callback: Any = None
        self.ready = threading.Event()
        self.failure_code: str | None = None

    @staticmethod
    def is_f24(text: str, keycode: int | None = None) -> bool:
        return F24_FUNCTION_KEY in text or keycode == MOUSE_F24_KEYCODE

    @staticmethod
    def is_f22(text: str, keycode: int | None = None) -> bool:
        return F22_FUNCTION_KEY in text or keycode == MOUSE_F22_KEYCODE

    @staticmethod
    def is_f23(text: str, keycode: int | None = None) -> bool:
        return F23_FUNCTION_KEY in text or keycode == MOUSE_F23_KEYCODE

    def run(
        self,
        stop_event: threading.Event,
        on_f22: Callable[[], None],
        on_f23: Callable[[], None],
        on_f24: Callable[[], None],
        on_command_f23: Callable[[], None],
        on_command_f24: Callable[[], None],
    ) -> None:
        if sys.platform != "darwin":
            self.failure_code = "unsupported_platform"
            LOGGER.error("F24 hotkey is only available on macOS")
            return

        application_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._core_foundation = core_foundation
        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        application_services.CGEventKeyboardGetUnicodeString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_uint16),
        ]
        application_services.CGEventGetIntegerValueField.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        application_services.CGEventGetIntegerValueField.restype = ctypes.c_longlong
        application_services.CGEventGetFlags.argtypes = [ctypes.c_void_p]
        application_services.CGEventGetFlags.restype = ctypes.c_ulonglong

        def handle_event(proxy: int, event_type: int, event: int, context: int) -> int:
            if event_type in (0xFFFFFFFE, 0xFFFFFFFF):
                self.failure_code = "event_tap_timeout" if event_type == 0xFFFFFFFE else "event_tap_user_disabled"
                self.ready.clear()
                trace("HOTKEY_LISTENER_DISABLED", reason=self.failure_code)
            if event_type == self.KEY_DOWN_EVENT:
                keycode = application_services.CGEventGetIntegerValueField(event, self.KEYCODE_FIELD)
                repeated = application_services.CGEventGetIntegerValueField(
                    event, self.AUTOREPEAT_FIELD
                )
                length = ctypes.c_ulong()
                characters = (ctypes.c_uint16 * 4)()
                application_services.CGEventKeyboardGetUnicodeString(
                    event,
                    len(characters),
                    ctypes.byref(length),
                    characters,
                )
                text = "".join(chr(characters[index]) for index in range(length.value))
                if not repeated:
                    flags = application_services.CGEventGetFlags(event)
                    if self.is_f22(text, keycode):
                        on_f22()
                    elif self.is_f23(text, keycode):
                        if flags & self.COMMAND_FLAG_MASK:
                            on_command_f23()
                        else:
                            on_f23()
                    elif self.is_f24(text, keycode):
                        if flags & self.COMMAND_FLAG_MASK:
                            on_command_f24()
                        else:
                            on_f24()
            return event

        self._callback = callback_type(handle_event)
        application_services.CGEventTapCreate.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_ulonglong,
            callback_type,
            ctypes.c_void_p,
        ]
        application_services.CGEventTapCreate.restype = ctypes.c_void_p
        event_tap = application_services.CGEventTapCreate(
            1,
            0,
            1,
            1 << self.KEY_DOWN_EVENT,
            self._callback,
            None,
        )
        if not event_tap:
            self.failure_code = "input_permission_or_tap_unavailable"
            LOGGER.error(
                "cannot listen for F24; grant Input Monitoring permission to Python/Terminal"
            )
            return

        core_foundation.CFMachPortCreateRunLoopSource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
        ]
        core_foundation.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
        core_foundation.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        core_foundation.CFRunLoopAddSource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        core_foundation.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

        source = core_foundation.CFMachPortCreateRunLoopSource(None, event_tap, 0)
        run_loop = core_foundation.CFRunLoopGetCurrent()
        self._run_loop = int(run_loop)
        common_modes = ctypes.c_void_p.in_dll(
            core_foundation, "kCFRunLoopCommonModes"
        ).value
        core_foundation.CFRunLoopAddSource(run_loop, source, common_modes)
        self.ready.set()
        LOGGER.info("F22-F24 hotkey listener started")
        try:
            if not stop_event.is_set():
                core_foundation.CFRunLoopRun()
        finally:
            self._run_loop = None
            core_foundation.CFRelease(source)
            core_foundation.CFRelease(event_tap)

    def stop(self) -> None:
        if self._run_loop and self._core_foundation:
            self._core_foundation.CFRunLoopStop(ctypes.c_void_p(self._run_loop))


def sender_readiness(config: Config, client: Any = None) -> dict[str, Any]:
    status: dict[str, Any] = {"service": "lanshot-sender", "build": BUILD,
                              "local": "ready", "receiver": "unknown", "capture_permission": "unverified"}
    try:
        private_dir(config.spool_dir)
        with tempfile.NamedTemporaryFile(dir=config.spool_dir):
            pass
        if not os.access(SCREEN_CAPTURE_EXECUTABLE, os.X_OK):
            status["local"] = "capture_executable_unavailable"
        redactor = NativeRedactor(config.blocked_words_file)
        if redactor._load_words() and not os.access(redactor.executable, os.X_OK):
            status["local"] = "redactor_unavailable"
    except (OSError, CaptureError) as error:
        status["local"] = "spool_or_redaction_config_unreadable"
        trace("READINESS_STORAGE_FAILED", **error_info(error))
    try:
        if client is not None:
            health = client.health()
        elif config.routes_file:
            from multi_receiver import build_client
            probe = build_client(config,start_embedded=False)
            try:
                health = probe.health()
            finally:
                probe.close()
        else:
            health = HTTPClient(config).health()
        status["receiver"] = "ready"
        status["model"] = health.get("analyzer", "unknown")
    except Exception as error:
        status["receiver"] = "unavailable_or_incompatible"
        trace("READINESS_RECEIVER_FAILED", **error_info(error))
    status["legacy_pending"] = len(list(config.spool_dir.glob("*.pending.json"))) if config.spool_dir.exists() else 0
    return status


class SenderService:
    def __init__(self, config: Config, screenshotter: Screenshotter, client: HTTPClient,
                 hotkey_listener: MacF24Listener | None = None,
                 page_command_path: Path = DEFAULT_PAGE_COMMAND_PATH,
                 written_mode: bool = False, voice_controller: "VoiceController | None" = None,
                 store: TaskStore | None = None) -> None:
        self.config, self.screenshotter, self.client = config, screenshotter, client
        self.stop_event, self.wake = threading.Event(), threading.Event()
        # Only native capture/redaction is serialized. NEVER hold this lock over HTTP.
        self.capture_lock, self._hotkey_guard = threading.Lock(), threading.Lock()
        self._hotkey_capture_running = False
        self.hotkey_listener = hotkey_listener or MacF24Listener()
        self.page_command_path = Path(page_command_path).expanduser()
        self.written_mode, self.voice_controller = written_mode, voice_controller
        self.store = store or TaskStore(config.spool_dir / "sender_tasks.sqlite3",
                                       max_items=config.queue_max_items, max_bytes=config.queue_max_bytes,
                                       ttl=config.queue_ttl_seconds)
        self.heartbeat = Heartbeat(config.spool_dir / "sender_status.json", "lanshot-sender")
        self.threads: list[threading.Thread] = []
        self.fault = threading.Event()
        self.last_capture: dict[str, Any] = {}
        self.receiver_state = "not_checked"
        target = getattr(client, "queue_target", None)
        self.queue_target = target if isinstance(target,str) else config.server_url
        budget = getattr(client, "operation_budget", None)
        self.upload_budget = budget if isinstance(budget,(int,float)) else config.request_timeout_seconds + 15

    def _guarded(self, name: str, function: Callable[[], None]) -> None:
        try:
            function()
        except Exception as error:
            trace("WORKER_FAILED", component=name, **error_info(error))
            self.heartbeat.progress(name, "failed")
            self.fault.set()
            self.stop_event.set()
            self.wake.set()

    def run(self) -> None:
        checks = sender_readiness(self.config,self.client) if not self.stop_event.is_set() else {"local": "not_checked"}
        self.heartbeat.write("starting", checks=checks)
        callbacks = ((self._trigger_hotkey_capture, lambda: self._trigger_voice_control("previous"),
                      lambda: self._trigger_voice_control("next")) if self.written_mode
                     else (self._trigger_hotkey_capture, self._trigger_page_up, self._trigger_page_down))
        self.threads = [
            threading.Thread(target=lambda: self._guarded("probe", self._probe_loop), name="health-probe", daemon=True),
            threading.Thread(target=lambda: self._guarded("poll", self._poll_loop), name="remote-poll", daemon=True),
            threading.Thread(target=lambda: self._guarded("upload", self._retry_loop), name="upload-worker", daemon=True),
            threading.Thread(target=lambda: self._guarded("hotkeys", lambda: self.hotkey_listener.run(
                self.stop_event, *callbacks, self._trigger_toggle_overlay, self._trigger_move_to_mouse)), name="hotkeys", daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        try:
            while not self.stop_event.wait(1):
                stalled = self.heartbeat.stalled()
                listener_error = getattr(self.hotkey_listener, "failure_code", None)
                snapshot = self.store.snapshot(include_answers=False)
                ready_flag = getattr(self.hotkey_listener, "ready", None)
                input_ready = ready_flag.is_set() if isinstance(ready_flag, threading.Event) else False
                self.heartbeat.write("degraded" if stalled or listener_error or not input_ready or self.receiver_state != "ready" or checks.get("local") != "ready" else "running",
                                     stalled=stalled, input_error=listener_error,
                                     input_ready=input_ready, receiver=self.receiver_state,
                                     last_capture=dict(self.last_capture), queue=snapshot, checks=checks)
                if stalled:
                    trace("WORKER_STALLED", components=stalled)
                    self.fault.set()
                    break
        finally:
            self.stop()
            for thread in self.threads:
                thread.join(timeout=1)
            self.heartbeat.write("failed" if self.fault.is_set() else "stopped")
        if self.fault.is_set():
            raise RuntimeError("service worker failed or exceeded its deadline")

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        self.hotkey_listener.stop()

    def _capture(self, task_id: uuid.UUID, kind: str) -> str:
        # A leased remote request can arrive again while its original upload waits.
        if self.store.get(str(task_id)) is not None:
            trace("CAPTURE_ALREADY_QUEUED", task_id)
            return str(task_id)
        with self.capture_lock:
            if self.store.get(str(task_id)) is not None:
                return str(task_id)
            self.last_capture = {"id": str(task_id), "state": "capturing", "at": time.time()}
            self.heartbeat.progress("capture", "capturing", deadline=90)
            image_path: Path | None = None
            try:
                image_path = self.screenshotter.capture(task_id)
                self.store.enqueue(task_id, image_path.read_bytes(), kind=kind,
                                   target=self.queue_target, profile=self.config.profile)
                self.last_capture = {"id": str(task_id), "state": "queued", "at": time.time()}
                trace("QUEUE_PERSISTED", task_id, kind=kind)
                # The durable database now owns a byte-identical redacted payload.
                image_path.unlink(missing_ok=True)
                self.wake.set()
                return str(task_id)
            except Exception as error:
                self.last_capture = {"id": str(task_id), "state": "failed", "code": type(error).__name__, "at": time.time()}
                trace("CAPTURE_OR_ENQUEUE_FAILED", task_id, **error_info(error))
                # Do not delete a successfully captured/redacted file on enqueue failure.
                raise
            finally:
                self.heartbeat.progress("capture", "idle")

    def handle_manual_capture(self, capture_id: uuid.UUID | None = None) -> str:
        capture_id = capture_id or uuid.uuid4()
        trace("MANUAL_CAPTURE_START", capture_id)
        return self._capture(capture_id, "scheduled")

    def handle_remote_task(self, task_id: uuid.UUID) -> None:
        trace("REMOTE_TASK_RECEIVED", task_id)
        try:
            self._capture(task_id, "remote")
        except CaptureError:
            self._report_capture_failure(task_id, "capture_failed")
        except QueueFull:
            self._report_capture_failure(task_id, "sender_queue_full")

    def _deliver_pending(self, job: dict[str, Any]) -> None:
        trace("UPLOAD_START", job["id"], attempt=job["attempts"])
        self.heartbeat.progress("upload", "uploading", deadline=self.upload_budget)
        try:
            self.client.upload_record(job)
            self.store.delivered(job)
            # This is NOT TASK_DONE. Receiver analysis and display have separate states.
            trace("UPLOAD_ACKNOWLEDGED", job["id"])
        except Exception as error:
            if isinstance(error, LeaseLost):
                trace("UPLOAD_STALE_LEASE", job["id"])
                return
            decision = retry_decision(error, job["attempts"], max_attempts=self.config.upload_max_attempts)
            from multi_receiver import OutcomeUnknown
            if isinstance(error, OutcomeUnknown) and decision.action != "retry":
                decision = RetryDecision("uncertain", "receiver_ack_unknown_route_remains_pinned")
            self.store.release(job, decision)
            trace("UPLOAD_RETRY_WAIT" if decision.action == "retry" else "UPLOAD_PAUSED",
                  job["id"], code=decision.code, delay=decision.delay, **error_info(error))
        finally:
            self.heartbeat.progress("upload", "idle")

    def _retry_pending_once(self) -> bool:
        job = self.store.claim(target=self.queue_target, profile=self.config.profile,
                               lease_seconds=self.upload_budget + 30)
        if job is None:
            return False
        self._deliver_pending(job)
        return True

    def _retry_loop(self) -> None:
        while not self.stop_event.is_set():
            self.heartbeat.progress("upload", "checking_queue", deadline=15)
            if not self._retry_pending_once():
                self.heartbeat.progress("upload", "idle")
                self.wake.wait(0.5)
                self.wake.clear()

    def _probe_loop(self) -> None:
        while not self.stop_event.is_set():
            self.heartbeat.progress("probe", "checking_receiver", deadline=self.upload_budget)
            try:
                self.client.health()
                self.receiver_state = "ready"
            except Exception as error:
                self.receiver_state = "unavailable_or_incompatible"
                trace("RECEIVER_HEALTH_FAILED", **error_info(error))
            finally:
                self.heartbeat.progress("probe", "idle")
            self.stop_event.wait(10)

    def _poll_loop(self) -> None:
        while not self.stop_event.is_set():
            self.heartbeat.progress("poll", "waiting_receiver", deadline=(self.config.poll_timeout_seconds + self.config.request_timeout_seconds + 15) * (len(self.config.retry_delays) + 1))
            try:
                task_id = self.client.poll_task()
                if task_id is not None:
                    self.heartbeat.progress("poll", "waiting_capture_slot")
                    self.handle_remote_task(task_id)
            except Exception as error:
                trace("POLL_FAILED", **error_info(error))
                self.stop_event.wait(2)
            finally:
                self.heartbeat.progress("poll", "idle")

    def _trigger_hotkey_capture(self) -> None:
        capture_id = uuid.uuid4()
        with self._hotkey_guard:
            if self._hotkey_capture_running or self.stop_event.is_set():
                trace("HOTKEY_IGNORED_BUSY", capture_id)
                return
            self._hotkey_capture_running = True
        trace("HOTKEY_RECEIVED", capture_id)
        def capture() -> None:
            try:
                self.handle_manual_capture(capture_id)
            except Exception as error:
                trace("MANUAL_CAPTURE_FAILED", capture_id, **error_info(error))
            finally:
                with self._hotkey_guard:
                    self._hotkey_capture_running = False
        threading.Thread(target=capture, name="hotkey-capture", daemon=True).start()

    def _trigger_voice_control(self, action: str) -> None:
        if self.stop_event.is_set() or self.voice_controller is None:
            return
        def control() -> None:
            try:
                self.voice_controller.control(action)
                trace("VOICE_CONTROL", action=action)
            except Exception as error:
                trace("VOICE_CONTROL_FAILED", **error_info(error))
        threading.Thread(target=control, name=f"voice-{action}", daemon=True).start()

    def _trigger_page_down(self) -> None:
        self._trigger_page_command("down")

    def _trigger_page_up(self) -> None:
        self._trigger_page_command("up")

    def _trigger_move_to_mouse(self) -> None:
        self._trigger_page_command("mouse")

    def _trigger_toggle_overlay(self) -> None:
        self._trigger_page_command("toggle")

    def _trigger_page_command(self, direction: str) -> None:
        if self.stop_event.is_set():
            return
        from reliability import atomic_write
        atomic_write(self.page_command_path, f"{direction} {time.time_ns()}\n".encode())
        trace("PAGE_CONTROL", direction=direction)

    def _report_capture_failure(self, task_id: uuid.UUID, code: str) -> None:
        try:
            self.client.report_failure(task_id, code, code)
        except Exception as error:
            trace("CAPTURE_FAILURE_REPORT_FAILED", task_id, **error_info(error))


class VoiceController:
    def __init__(
        self,
        url: str,
        token: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.url = url.rstrip("/") + "/api/control"
        self.token = token
        self.opener = opener

    def control(self, action: str) -> None:
        if action not in {"previous", "next", "replay"}:
            raise ValueError("unsupported voice action")
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"action": action}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Voice-Token": self.token,
            },
            method="POST",
        )
        with self.opener(request, timeout=10) as response:
            if response.status != HTTPStatus.OK:
                raise ProtocolError(f"voice server returned HTTP {response.status}")
            response.read(MAX_RESPONSE_BYTES)

def build_launch_agent_plist(
    python_path: Path,
    script_path: Path,
    config_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(python_path),
            str(script_path),
            "run",
            "--config",
            str(config_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def install_launch_agent(config_path: Path) -> None:
    raise ConfigError("Legacy install is disabled in P1. Use manage_services.py configure/start; existing P0 agents are untouched.")


def uninstall_launch_agent() -> None:
    raise ConfigError("Legacy uninstall is disabled in P1. Use manage_services.py stop/uninstall for P1 agents only.")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )


def run_service(config_path: Path, written_mode: bool = False) -> None:
    config = Config.load(config_path)
    screenshotter = Screenshotter(
        config.spool_dir,
        include_cursor=config.include_cursor,
        redactor=NativeRedactor(config.blocked_words_file),
    )
    voice_controller = None
    if written_mode:
        voice_url = os.environ.get("LANSHOT_VOICE_URL", "").strip()
        voice_token = os.environ.get("LANSHOT_VOICE_TOKEN", "").strip()
        if not voice_url or not voice_token:
            raise ConfigError("written mode requires LANSHOT_VOICE_URL and LANSHOT_VOICE_TOKEN")
        voice_controller = VoiceController(voice_url, voice_token)
    with InstanceLock(config.spool_dir / "sender.lock"):
        if config.routes_file:
            from multi_receiver import build_client
            client = build_client(config)
        else:
            client = HTTPClient(config)
        service = SenderService(config, screenshotter, client,
            page_command_path=(config.display_dir / "page.txt") if config.display_dir else DEFAULT_PAGE_COMMAND_PATH,
            written_mode=written_mode, voice_controller=voice_controller)

        def stop_service(signum: int, frame: object) -> None:
            LOGGER.info("received signal %s", signum)
            service.stop()

        signal.signal(signal.SIGTERM, stop_service)
        signal.signal(signal.SIGINT, stop_service)
        try:
            service.run()
        finally:
            if config.routes_file:
                client.close()


def capture_test(output: Path, include_cursor: bool) -> None:
    output = output.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="lanshot-capture-") as directory:
        captured = Screenshotter(
            Path(directory),
            include_cursor=include_cursor,
        ).capture(uuid.uuid4())
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(captured, output)
    print(f"captured: {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LanShot sender service")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="write the sender config")
    configure.add_argument("--server-url", required=True)
    configure.add_argument("--interval", type=float, default=0, help=argparse.SUPPRESS)
    configure.add_argument("--include-cursor", action="store_true")
    configure.add_argument("--spool-dir", type=Path, default=DEFAULT_SPOOL_DIR)
    configure.add_argument("--profile", default="default")
    configure.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    run = subparsers.add_parser("run", help="run in the foreground")
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run.add_argument("--written-mode", action="store_true")

    once = subparsers.add_parser("once", help="capture and upload once")
    once.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    capture = subparsers.add_parser("capture", help="test local screen capture")
    capture.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "Desktop" / "lanshot-test.jpg",
    )
    capture.add_argument("--include-cursor", action="store_true")

    install = subparsers.add_parser("install", help="install the LaunchAgent")
    install.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    doctor = subparsers.add_parser("doctor", help="local readiness; --capture tests capture without uploading")
    doctor.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    doctor.add_argument("--capture", action="store_true")
    doctor.add_argument("--receiver-only", action="store_true")
    subparsers.add_parser("uninstall", help="remove the LaunchAgent")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    configure_logging()
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "configure":
            config = Config(
                server_url=arguments.server_url,
                interval_seconds=0,
                include_cursor=arguments.include_cursor,
                spool_dir=arguments.spool_dir, profile=arguments.profile,
            )
            config.write(arguments.config)
            config.blocked_words_file.parent.mkdir(parents=True, exist_ok=True)
            config.blocked_words_file.touch(mode=0o600, exist_ok=True)
            print(f"configured: {arguments.config.expanduser()}")
        elif arguments.command == "run":
            run_service(arguments.config, arguments.written_mode)
        elif arguments.command == "once":
            config = Config.load(arguments.config)
            if config.routes_file:
                from multi_receiver import build_client
                client = build_client(config,start_embedded=False)
            else:
                client = HTTPClient(config)
            try:
                SenderService(config, Screenshotter(config.spool_dir,config.include_cursor,
                    redactor=NativeRedactor(config.blocked_words_file)), client).handle_manual_capture()
            finally:
                if config.routes_file:
                    client.close()
            print("Capture persisted in the queue. The managed sender delivers it; upload is not analysis completion.")
        elif arguments.command == "doctor":
            config = Config.load(arguments.config)
            status = sender_readiness(config)
            if arguments.capture:
                with tempfile.TemporaryDirectory(prefix="lanshot-local-test-") as directory:
                    Screenshotter(Path(directory), config.include_cursor,
                                  redactor=NativeRedactor(config.blocked_words_file)).capture(uuid.uuid4())
                status["capture_test"] = "passed_locally_not_uploaded"
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["receiver"] == "ready" and (arguments.receiver_only or status["local"] == "ready") else 1
        elif arguments.command == "capture":
            capture_test(arguments.output, arguments.include_cursor)
        elif arguments.command == "install":
            install_launch_agent(arguments.config)
        elif arguments.command == "uninstall":
            uninstall_launch_agent()
        return 0
    except (ConfigError, CaptureError, ProtocolError, OSError, subprocess.SubprocessError, Conflict, QueueFull, sqlite3.Error) as error:
        trace("COMMAND_FAILED", **error_info(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
