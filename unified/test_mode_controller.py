import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mode_controller import AUDIO_EXECUTABLE, OVERLAY_EXECUTABLE, ModeController, ModeError


class FakeRunner:
    def __init__(self, *, audio_start_code=0, screenshot_start_code=0, godhands=False):
        self.audio_start_code = audio_start_code
        self.screenshot_start_code = screenshot_start_code
        self.godhands = godhands
        self.calls = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        text = " ".join(command)
        if "pgrep" in text:
            if ("[g]odhands" in text.lower() or "GodHands.app" in text) and self.godhands:
                return subprocess.CompletedProcess(command, 0, "123\n", "")
            return subprocess.CompletedProcess(command, 1, "", "")
        if "audio_service.py prepare" in text:
            return subprocess.CompletedProcess(command, self.audio_start_code, "audio started", "audio failed")
        if "manage_services.py start" in text:
            return subprocess.CompletedProcess(command, self.screenshot_start_code, "screenshot started", "screenshot failed")
        return subprocess.CompletedProcess(command, 0, "stopped", "")


class ModeControllerTests(unittest.TestCase):
    def test_portable_installer_builds_voice_mode_without_storing_key(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "install.command").read_text(encoding="utf-8")
        runtime = (root / "unified/runtime_python.zsh").read_text(encoding="utf-8")
        self.assertIn("security add-generic-password", installer)
        self.assertIn("com.lanshot.google", installer)
        self.assertIn("GEMINI_API_KEY", installer)
        self.assertIn("capture-exclusion-demo/build.sh", installer)
        self.assertIn("screenshot-sender/build_native_ocr.command", installer)
        self.assertIn("lanshot2/build_audio.command", installer)
        self.assertIn("mode_controller.py\" voice", installer)
        self.assertIn("configure_knowledge.command", installer)
        self.assertNotRegex(installer, r"sk-[A-Za-z0-9]{16,}")
        self.assertIn("sys.version_info < (3, 10)", runtime)
        self.assertIn("/opt/homebrew/opt/python@3.12/libexec/bin/python3", runtime)
        self.assertIn("/usr/local/opt/python@3.12/libexec/bin/python3", runtime)
        for name in (
            "LanShot.command",
            "voice_mode.command",
            "stop_all.command",
            "configure_knowledge.command",
            "test_knowledge.command",
        ):
            launcher = (root / "unified" / name).read_text(encoding="utf-8")
            self.assertIn("runtime_python.zsh", launcher)

    def test_voice_executable_uses_final_app_identity(self):
        self.assertEqual(
            AUDIO_EXECUTABLE.parts[-4:],
            ("LanShot Voice Capture.app", "Contents", "MacOS", "native_audio_capture"),
        )
        self.assertEqual(
            OVERLAY_EXECUTABLE.parts[-4:],
            ("CaptureExclusionDemo.app", "Contents", "MacOS", "CaptureExclusionDemo"),
        )

    def controller(self, directory, runner):
        settings = Path(directory) / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        controller = ModeController(
            settings,
            Path(directory) / "state",
            runner=runner,
            sleeper=lambda _: None,
        )
        controller._audio_running = lambda: False
        controller._voice_overlay_running = lambda: False
        controller._voice_control_running = lambda: False
        controller._voice_hotkey_ready = lambda: False
        return controller

    def test_screenshot_mode_stops_audio_before_starting_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            controller = self.controller(directory, runner)
            result = controller.switch("screenshot")
            self.assertEqual(result["status"], "ready")
            calls = [" ".join(call) for call in runner.calls]
            self.assertLess(next(i for i, call in enumerate(calls) if "audio_service.py stop" in call),
                            next(i for i, call in enumerate(calls) if "manage_services.py start" in call))
            self.assertEqual(json.loads(controller.state_file.read_text())["mode"], "screenshot")

    def test_voice_mode_stops_screenshot_before_starting_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            controller = self.controller(directory, runner)
            result = controller.switch("voice")
            self.assertEqual(result["status"], "idle")
            calls = [" ".join(call) for call in runner.calls]
            self.assertLess(next(i for i, call in enumerate(calls) if "manage_services.py stop" in call),
                            next(i for i, call in enumerate(calls) if "audio_service.py prepare" in call))

    def test_voice_mode_does_not_require_screenshot_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = ModeController(
                Path(directory) / "missing-settings.json",
                Path(directory) / "state",
                runner=FakeRunner(),
                sleeper=lambda _: None,
            )
            controller._audio_running = lambda: False
            controller._voice_overlay_running = lambda: False
            controller._voice_control_running = lambda: False
            controller._voice_hotkey_ready = lambda: False

            self.assertEqual(controller.switch("voice")["status"], "idle")

    def test_failed_voice_start_does_not_claim_voice_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, FakeRunner(audio_start_code=1))
            with self.assertRaises(ModeError):
                controller.switch("voice")
            state = json.loads(controller.state_file.read_text())
            self.assertEqual((state["mode"], state["status"]), ("stopped", "failed"))

    def test_voice_ui_without_capture_is_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, FakeRunner())
            controller._voice_overlay_running = lambda: True
            controller._voice_control_running = lambda: True
            result = controller.status()
            self.assertEqual((result["mode"], result["status"]), ("voice", "idle"))

    def test_godhands_conflict_is_rejected_before_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, FakeRunner(godhands=True))
            with self.assertRaises(ModeError):
                controller.switch("screenshot")


if __name__ == "__main__":
    unittest.main()
