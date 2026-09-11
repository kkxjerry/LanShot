#!/usr/bin/env python3
"""LanShot unified mode controller: screenshot and voice modes are exclusive."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "screenshot-sender" / "manage_services.py"
AUDIO_SERVICE = ROOT / "lanshot2" / "audio_service.py"
AUDIO_BUILD = ROOT / "lanshot2" / "build_audio.command"
AUDIO_EXECUTABLE = ROOT / "lanshot2" / "LanShot2AudioCapture.app/Contents/MacOS/native_audio_capture"
DEFAULT_SETTINGS = Path.home() / "Library/Application Support/LanShotP1R2Live/settings.json"
DEFAULT_STATE_DIR = Path.home() / "Library/Application Support/LanShotUnified"


class ModeError(RuntimeError):
    pass


class ModeController:
    def __init__(
        self,
        settings: Path = DEFAULT_SETTINGS,
        state_dir: Path = DEFAULT_STATE_DIR,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings.expanduser().resolve()
        self.state_dir = state_dir.expanduser().resolve()
        self.state_file = self.state_dir / "mode.json"
        self.runner = runner
        self.sleeper = sleeper

    def _run(self, command: list[str], timeout: float = 45) -> subprocess.CompletedProcess:
        return self.runner(command, capture_output=True, text=True, check=False, timeout=timeout)

    def _write_state(self, mode: str, status: str, message: str = "") -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "mode": mode,
            "status": status,
            "message": message,
            "updated_at": time.time(),
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.state_dir, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
            os.chmod(self.state_file, 0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _godhands_running(self) -> bool:
        result = self._run(["/usr/bin/pgrep", "-f", "[g]odhands|[G]odHands.app"])
        return result.returncode == 0 and bool(result.stdout.strip())

    def _audio_running(self) -> bool:
        pid_file = Path.home() / "Library/Application Support/LanShot2/audio/capture.pid"
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def _screenshot_running(self) -> bool:
        result = self._run([
            "/usr/bin/pgrep", "-f",
            f"[m]anage_services.py run-child .*{self.settings}",
        ])
        return result.returncode == 0 and bool(result.stdout.strip())

    def _stop_audio(self) -> subprocess.CompletedProcess:
        return self._run([sys.executable, str(AUDIO_SERVICE), "stop"], timeout=70)

    def _stop_screenshot(self) -> subprocess.CompletedProcess | None:
        if not self.settings.is_file():
            return None
        return self._run([
            sys.executable, str(MANAGER), "stop", "--settings", str(self.settings)
        ], timeout=30)

    def _wait_stopped(self, check: Callable[[], bool], timeout: float = 8) -> bool:
        deadline = time.monotonic() + timeout
        while check() and time.monotonic() < deadline:
            self.sleeper(0.2)
        return not check()

    def switch(self, mode: str) -> dict:
        if mode not in {"screenshot", "voice"}:
            raise ValueError("mode must be screenshot or voice")
        if self._godhands_running():
            raise ModeError("GodHands 正在运行，请先退出，避免音频和快捷键冲突")
        if not self.settings.is_file():
            raise ModeError(f"找不到 LanShot 设置：{self.settings}")

        if mode == "screenshot":
            self._stop_audio()
            if not self._wait_stopped(self._audio_running):
                self._write_state("stopped", "failed", "语音模式未能停止")
                raise ModeError("语音模式未能停止，拒绝同时启动截屏模式")
            result = self._run([
                sys.executable, str(MANAGER), "start",
                "--settings", str(self.settings),
                "--expected-profile", "default",
                "--open-display",
                "--reset-budget",
            ])
            if result.returncode not in (0, 2):
                self._write_state("stopped", "failed", "截图模式启动失败")
                raise ModeError((result.stderr or result.stdout or "截图模式启动失败").strip())
            status = "ready" if result.returncode == 0 else "degraded"
            self._write_state(mode, status, "截屏、AI和悬浮窗已启动")
            return {"mode": mode, "status": status, "detail": result.stdout.strip()}

        self._stop_screenshot()
        if not self._wait_stopped(self._screenshot_running):
            self._write_state("stopped", "failed", "截屏模式未能停止")
            raise ModeError("截屏模式未能停止，拒绝同时启动语音模式")
        if not AUDIO_EXECUTABLE.is_file():
            build = self._run([str(AUDIO_BUILD)], timeout=90)
            if build.returncode != 0:
                self._write_state("stopped", "failed", "语音辅助程序构建失败")
                raise ModeError((build.stderr or build.stdout or "语音辅助程序构建失败").strip())
        result = self._run([sys.executable, str(AUDIO_SERVICE), "start"], timeout=40)
        if result.returncode != 0:
            self._write_state("stopped", "failed", "语音模式启动失败")
            raise ModeError((result.stderr or result.stdout or "语音模式启动失败").strip())
        self._write_state(mode, "ready", "系统音频和麦克风双路实时ASR已启动")
        return {"mode": mode, "status": "ready", "detail": result.stdout.strip()}

    def stop(self) -> dict:
        audio = self._stop_audio()
        screenshot = self._stop_screenshot()
        self._wait_stopped(self._audio_running)
        self._wait_stopped(self._screenshot_running)
        remaining = {
            "voice": self._audio_running(),
            "screenshot": self._screenshot_running(),
        }
        status = "stopped" if not any(remaining.values()) else "degraded"
        self._write_state("stopped", status, "所有模式已停止" if status == "stopped" else "仍有组件未退出")
        return {
            "mode": "stopped",
            "status": status,
            "remaining": remaining,
            "audio": audio.stdout.strip(),
            "screenshot": screenshot.stdout.strip() if screenshot else "settings_missing",
        }

    def status(self) -> dict:
        voice = self._audio_running()
        screenshot = self._screenshot_running()
        if voice and screenshot:
            mode, status = "conflict", "degraded"
        elif voice:
            mode, status = "voice", "running"
        elif screenshot:
            mode, status = "screenshot", "running"
        else:
            mode, status = "stopped", "stopped"
        return {
            "mode": mode,
            "status": status,
            "voice_running": voice,
            "screenshot_running": screenshot,
            "godhands_running": self._godhands_running(),
            "audio_directory": str(Path.home() / "Library/Application Support/LanShot2/audio"),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="LanShot 截屏/语音模式控制器")
    parser.add_argument("mode", choices=("screenshot", "voice", "stop", "status"))
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    args = parser.parse_args()
    controller = ModeController(settings=args.settings)
    try:
        result = controller.status() if args.mode == "status" else (
            controller.stop() if args.mode == "stop" else controller.switch(args.mode)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "degraded" else 0
    except (ModeError, ValueError) as error:
        print(json.dumps({"status": "failed", "message": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
