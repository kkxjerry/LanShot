import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mode_controller import AUDIO_EXECUTABLE, ModeController, ModeError


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
        if "audio_service.py start" in text:
            return subprocess.CompletedProcess(command, self.audio_start_code, "audio started", "audio failed")
        if "manage_services.py start" in text:
            return subprocess.CompletedProcess(command, self.screenshot_start_code, "screenshot started", "screenshot failed")
        return subprocess.CompletedProcess(command, 0, "stopped", "")


class ModeControllerTests(unittest.TestCase):
    def test_voice_executable_uses_final_app_identity(self):
        self.assertEqual(
            AUDIO_EXECUTABLE.parts[-4:],
            ("LanShot Voice Capture.app", "Contents", "MacOS", "native_audio_capture"),
        )

    def controller(self, directory, runner):
        settings = Path(directory) / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        return ModeController(settings, Path(directory) / "state", runner=runner, sleeper=lambda _: None)

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
            self.assertEqual(result["status"], "ready")
            calls = [" ".join(call) for call in runner.calls]
            self.assertLess(next(i for i, call in enumerate(calls) if "manage_services.py stop" in call),
                            next(i for i, call in enumerate(calls) if "audio_service.py start" in call))

    def test_failed_voice_start_does_not_claim_voice_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, FakeRunner(audio_start_code=1))
            with self.assertRaises(ModeError):
                controller.switch("voice")
            state = json.loads(controller.state_file.read_text())
            self.assertEqual((state["mode"], state["status"]), ("stopped", "failed"))

    def test_godhands_conflict_is_rejected_before_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory, FakeRunner(godhands=True))
            with self.assertRaises(ModeError):
                controller.switch("screenshot")


if __name__ == "__main__":
    unittest.main()
