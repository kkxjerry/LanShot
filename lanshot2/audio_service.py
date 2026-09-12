#!/usr/bin/env python3
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "LanShot Voice Capture.app"
OVERLAY_APP = ROOT.parent / "capture-exclusion-demo/build/CaptureExclusionDemo.app"
OVERLAY_EXECUTABLE = OVERLAY_APP / "Contents/MacOS/CaptureExclusionDemo"
DEFAULT_OUTPUT = Path.home() / "Library/Application Support/LanShot2/audio"
VOICE_PROMPT = ROOT / "voice_question_prompt.txt"
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VOICE_MODEL = "kimi-k2.7-code"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def load_api_key() -> str:
    environment_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if environment_key:
        return environment_key
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
    if not key:
        raise RuntimeError("未找到百炼 API Key")
    return key


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

    def ask(self, question: str, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            "enable_thinking": False,
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
            with self.opener(request, timeout=120) as response:
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
        return answer.strip().replace("```python", "").replace("```", "").strip()


def submit_question(
    output: Path,
    *,
    client: VoiceQuestionClient | None = None,
) -> bool:
    question = read_text(output / "interviewer.txt")
    if not question:
        atomic_text(output / "answer.txt", "没有识别到面试官的问题，请重新采集。\n")
        return False
    atomic_text(output / "question.txt", f"{question}\n")
    atomic_text(output / "answer.txt", "正在生成答案...\n")
    atomic_text(
        output / "question_status.json",
        json.dumps({"status": "requesting", "at": time.time()}, ensure_ascii=False) + "\n",
    )
    try:
        prompt = VOICE_PROMPT.read_text(encoding="utf-8").strip()
        active_client = client or VoiceQuestionClient(load_api_key())
        answer = active_client.ask(question, prompt)
    except (OSError, RuntimeError) as error:
        message = f"提问失败：{error}"
        atomic_text(output / "answer.txt", f"{message}\n")
        atomic_text(
            output / "question_status.json",
            json.dumps({"status": "failed", "message": str(error), "at": time.time()}, ensure_ascii=False)
            + "\n",
        )
        return False
    atomic_text(output / "answer.txt", f"{answer}\n")
    atomic_text(
        output / "question_status.json",
        json.dumps({"status": "complete", "model": VOICE_MODEL, "at": time.time()}, ensure_ascii=False)
        + "\n",
    )
    history = output.parent / "questions" / time.strftime("%Y-%m-%d")
    identifier = f"{time.strftime('%H%M%S')}-{time.time_ns()}"
    atomic_text(history / f"{identifier}-question.txt", f"{question}\n")
    atomic_text(history / f"{identifier}-answer.txt", f"{answer}\n")
    return True


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
    if action not in ("start", "stop", "quit"):
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
    if not process_id(output) and read_text(output / "capture.log") in ("", "running", "starting"):
        (output / "capture.log").write_text("stopped\n", encoding="utf-8")
    try:
        start_control(output)
        start_overlay(output)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        stop_control(output)
        print(str(error), file=sys.stderr)
        return 1
    print("语音模式已就绪，点击悬浮窗或菜单栏中的“开始采集”后才会录音")
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
    atomic_text(output / "answer.txt", "正在采集问题...\n")
    for name in ("question.txt", "question_status.json"):
        try:
            (output / name).unlink()
        except FileNotFoundError:
            pass
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
    control_stopped = stop_control(output)
    audio_stopped = stop_capture(output)
    overlay_stopped = stop_overlay(output)
    if not control_stopped or not audio_stopped or not overlay_stopped:
        print("停止超时", file=sys.stderr)
        return 1
    print("音频采集和语音悬浮窗已停止，文件已写完")
    return 0


def capture_stop(output: Path) -> int:
    was_running = process_id(output) is not None
    if not stop_capture(output):
        print("停止采集超时", file=sys.stderr)
        return 1
    if was_running:
        submit_question(output)
        print("采集已停止，问题已提交，悬浮窗继续保留")
    else:
        print("当前没有正在进行的采集")
    return 0


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

    def toggle_capture() -> None:
        state = read_text(output / "capture.log")
        action = "stop" if state in ("running", "starting") else "start"
        requested_state = "stopping" if action == "stop" else "starting"
        (output / "capture.log").write_text(f"{requested_state}\n", encoding="utf-8")
        write_capture_command(output, action)

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
            ignore_hotkey,
            toggle_capture,
            ignore_hotkey,
            ignore_hotkey,
            ignore_hotkey,
        ),
        name="lanshot-voice-f23",
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
                elif action == "quit":
                    break
            time.sleep(0.1)
    finally:
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
    commands = ("prepare", "start", "capture-stop", "stop", "status", "control-loop")
    parser.add_argument("command", choices=commands)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return {
        "prepare": prepare,
        "start": start,
        "capture-stop": capture_stop,
        "stop": stop,
        "status": status,
        "control-loop": control_loop,
    }[args.command](
        args.output.expanduser().resolve()
    )


if __name__ == "__main__":
    raise SystemExit(main())
