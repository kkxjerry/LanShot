#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "LanShot Voice Capture.app"
OVERLAY_APP = ROOT.parent / "capture-exclusion-demo/build/CaptureExclusionDemo.app"
OVERLAY_EXECUTABLE = OVERLAY_APP / "Contents/MacOS/CaptureExclusionDemo"
DEFAULT_OUTPUT = Path.home() / "Library/Application Support/LanShot2/audio"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


def start_overlay(output: Path) -> None:
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
        ["open", "-n", str(OVERLAY_APP), "--args", "--lanshot-voice-dir", str(output)],
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


def start(output: Path) -> int:
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
    pid = process_id(output)
    if not pid:
        overlay_stopped = stop_overlay(output)
        print("音频采集没有运行，语音悬浮窗已停止" if overlay_stopped else "语音悬浮窗停止超时")
        return 0 if overlay_stopped else 1
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process_id(output):
        time.sleep(0.2)
    audio_stopped = process_id(output) is None
    overlay_stopped = stop_overlay(output)
    if not audio_stopped or not overlay_stopped:
        print("停止超时", file=sys.stderr)
        return 1
    print("音频采集和语音悬浮窗已停止，文件已写完")
    return 0


def status(output: Path) -> int:
    pid = process_id(output)
    overlay_pid = overlay_process_id(output)
    state = read_text(output / "capture.log") or "未启动"
    if state == "running" and not pid:
        state = "failed: process exited unexpectedly"
    print(f"状态：{state}")
    print(f"进程：{pid if pid else '无'}")
    print(f"悬浮窗：{overlay_pid if overlay_pid else '无'}")
    print(f"目录：{output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LanShot2 双路实时音频采集和语音识别")
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return {"start": start, "stop": stop, "status": status}[args.command](
        args.output.expanduser().resolve()
    )


if __name__ == "__main__":
    raise SystemExit(main())
