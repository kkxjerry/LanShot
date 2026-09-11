import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_audio_service():
    spec = importlib.util.spec_from_file_location("lanshot2_audio_service", ROOT / "audio_service.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LanShot2Tests(unittest.TestCase):
    def test_uses_separate_runtime_and_app(self):
        service = load_audio_service()
        self.assertEqual(service.APP.name, "LanShot2AudioCapture.app")
        self.assertEqual(service.DEFAULT_OUTPUT.parts[-2:], ("LanShot2", "audio"))

    def test_native_capture_has_two_realtime_transcribers(self):
        source = (ROOT / "native_audio_capture.swift").read_text(encoding="utf-8")
        self.assertIn('appendingPathComponent("interviewer.txt")', source)
        self.assertIn('appendingPathComponent("me.txt")', source)
        self.assertIn("realtimeTranscriber.append(buffer)", source)
        self.assertIn("await (interviewerFinish, microphoneFinish)", source)
        self.assertIn('"qwen-audio-3.0-asr-flash-streaming"', source)
        self.assertIn('"wss://dashscope.aliyuncs.com/api-ws/v1/inference"', source)
        self.assertIn('"action": "run-task"', source)
        self.assertIn("socket.send(.data(data))", source)
        self.assertNotIn("input_audio_buffer.append", source)


if __name__ == "__main__":
    unittest.main()
