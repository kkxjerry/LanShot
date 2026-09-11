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
        self.assertEqual(service.APP.name, "LanShot Voice Capture.app")
        self.assertEqual(service.DEFAULT_OUTPUT.parts[-2:], ("LanShot2", "audio"))

    def test_background_capture_uses_app_identity(self):
        source = (ROOT / "audio_service.py").read_text(encoding="utf-8")
        self.assertIn('["open", "-n"', source)

    def test_build_requires_stable_development_identity(self):
        source = (ROOT / "build_audio.command").read_text(encoding="utf-8")
        self.assertIn("Apple Development:", source)
        self.assertIn("LANSHOT_CODESIGN_IDENTITY", source)
        self.assertNotIn("codesign --force --sign - ", source)

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
        self.assertIn("CGMainDisplayID()", source)
        self.assertIn('appendingPathComponent("interviewer.wav")', source)
        self.assertIn("Data(repeating: 0, count: 3_200)", source)
        self.assertIn("configuration.captureMicrophone = true", source)
        self.assertIn("type: .microphone", source)
        self.assertIn("disableAutomaticTermination", source)
        self.assertNotIn("import Speech", source)
        self.assertNotIn("requestSpeechAuthorization", source)

    def test_app_uses_final_stable_bundle_identity(self):
        plist = (ROOT / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("com.lanshot.unified.voice-capture", plist)
        self.assertIn("LanShot Voice Capture", plist)
        self.assertNotIn("NSSpeechRecognitionUsageDescription", plist)


if __name__ == "__main__":
    unittest.main()
