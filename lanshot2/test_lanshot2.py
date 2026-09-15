import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __iter__(self):
        return iter(self.body.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


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
        self.assertEqual(service.OVERLAY_APP.name, "CaptureExclusionDemo.app")
        self.assertEqual(service.DEFAULT_OUTPUT.parts[-2:], ("LanShot2", "audio"))

    def test_background_capture_uses_app_identity(self):
        source = (ROOT / "audio_service.py").read_text(encoding="utf-8")
        self.assertIn('["open", "-n"', source)
        self.assertIn('"--lanshot-voice-dir"', source)
        self.assertIn('"voice_overlay.pid"', source)
        self.assertIn('"voice_control.pid"', source)
        self.assertIn('"voice_overlay_command.txt"', source)
        self.assertIn('"voice_capture_command.txt"', source)
        self.assertIn('"capture-stop"', source)
        self.assertIn('"capture-submit"', source)
        self.assertIn('"control-loop"', source)
        self.assertIn('"new-session"', source)
        self.assertIn('"switch-screenshot"', source)
        self.assertIn("MacF24Listener", source)
        self.assertIn('"voice_hotkey_status.txt"', source)
        self.assertIn('write_capture_command(output, "submit")', source)
        self.assertIn("if not overlay_process_id(output):", source)

    def test_voice_overlay_shows_both_transcripts_and_menu_icon(self):
        source = (
            ROOT.parent / "capture-exclusion-demo/CaptureExclusionDemo.swift"
        ).read_text(encoding="utf-8")
        self.assertIn('"--lanshot-voice-dir"', source)
        self.assertIn('appendingPathComponent("interviewer.txt")', source)
        self.assertIn('appendingPathComponent("me.txt")', source)
        self.assertIn('appendingPathComponent("answer.txt")', source)
        self.assertIn('"mic.fill"', source)
        self.assertIn('bodyHeight * 0.35', source)
        self.assertIn('title: "开始采集"', source)
        self.assertIn('"F23 发送问题"', source)
        self.assertIn('title: "发送问题（F23）"', source)
        self.assertIn('title: "退出 LanShot"', source)
        self.assertIn('title: "会话管理..."', source)
        self.assertIn('title: "继续所选会话"', source)
        self.assertIn('title: "截屏模式"', source)
        self.assertIn('title: "面试模式"', source)
        self.assertIn("SessionManagerWindowController", source)
        self.assertIn('"shutdown \\(UUID().uuidString.lowercased())', source)
        self.assertIn("sharingType = .none", source)

    def test_voice_question_uses_text_only_glm_request(self):
        service = load_audio_service()
        response = {"choices": [{"message": {"content": "这是回答"}}]}
        opener = mock.Mock(return_value=FakeResponse(json.dumps(response).encode()))
        client = service.VoiceQuestionClient("secret-key", opener=opener)

        self.assertEqual(
            client.ask(
                "什么是 GIL？",
                "直接回答",
                history=[{"input": "上一题", "answer": "上一题答案"}],
            ),
            "这是回答",
        )
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertTrue(payload["enable_thinking"])
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "上一题"})
        self.assertEqual(payload["messages"][2], {"role": "assistant", "content": "上一题答案"})
        self.assertEqual(payload["messages"][3], {"role": "user", "content": "什么是 GIL？"})
        self.assertEqual(request.headers["Authorization"], "Bearer secret-key")

    def test_macos_keychain_wins_over_stale_environment_keys(self):
        service = load_audio_service()
        bailian_result = subprocess.CompletedProcess([], 0, "keychain-bailian\n", "")
        google_result = subprocess.CompletedProcess([], 0, "keychain-google\n", "")
        with mock.patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "stale-bailian",
                "GEMINI_API_KEY": "stale-google",
            },
        ):
            with mock.patch.object(service.subprocess, "run", return_value=bailian_result):
                self.assertEqual(service.load_api_key(), "keychain-bailian")
            with mock.patch.object(service.subprocess, "run", return_value=google_result):
                self.assertEqual(service.load_google_api_key(), "keychain-google")

    def test_gemini_question_uses_developer_api_high_thinking_stream(self):
        service = load_audio_service()
        first = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "不可显示的思考"},
                            {"text": "这是 "},
                        ]
                    },
                }
            ],
        }
        second = {
            "candidates": [
                {"content": {"parts": [{"text": "Gemini 回答"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"totalTokenCount": 123},
        }
        body = (
            "data: " + json.dumps(first, ensure_ascii=False) + "\n\n"
            "data: " + json.dumps(second, ensure_ascii=False) + "\n\n"
        ).encode()
        opener = mock.Mock(return_value=FakeResponse(body))
        client = service.GeminiQuestionClient("google-secret", opener=opener)
        updates = []

        answer = client.ask_stream(
            "本轮问题",
            "现场口答",
            history=[{"input": "上一题", "answer": "上一答"}],
            on_update=updates.append,
        )

        self.assertEqual(answer, "这是 Gemini 回答")
        self.assertEqual(updates, ["这是", "这是 Gemini 回答"])
        self.assertEqual(client.last_usage, {"totalTokenCount": 123})
        self.assertTrue(client.last_timing["streaming"])
        self.assertEqual(client.last_timing["event_count"], 2)
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, service.GEMINI_URL)
        self.assertEqual(request.headers["X-goog-api-key"], "google-secret")
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "现场口答")
        self.assertEqual(payload["contents"][0]["role"], "user")
        self.assertEqual(payload["contents"][1]["role"], "model")
        self.assertEqual(payload["contents"][-1]["parts"][0]["text"], "本轮问题")
        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "HIGH",
        )
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 4096)
        self.assertEqual(opener.call_args.kwargs["timeout"], 20)

    def test_gemini_failure_falls_back_to_glm(self):
        service = load_audio_service()
        primary = mock.Mock(model="gemini-3.8-flash")
        primary.ask_stream.side_effect = RuntimeError("offline")
        fallback = mock.Mock(model="glm-5.3")
        fallback.ask.return_value = "GLM 备用答案"
        client = service.FallbackQuestionClient(primary, fallback)

        self.assertEqual(client.ask("问题", "提示", history=[]), "GLM 备用答案")
        self.assertTrue(client.fallback_used)
        self.assertEqual(client.model, "glm-5.3")
        fallback.ask.assert_called_once_with("问题", "提示", history=[])

    def test_submission_writes_streaming_answer_before_completion(self):
        service = load_audio_service()

        class StreamingClient:
            model = "gemini-3.8-flash"
            fallback_used = False
            last_timing = {"first_visible_ms": 1200, "complete_ms": 1800}

            def ask_stream(self, question, prompt, history=None, on_update=None):
                on_update("第一句")
                self.partial_on_disk = (output / "answer.txt").read_text().strip()
                on_update("第一句\n第二句")
                return "第一句\n第二句"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir()
            (output / "interviewer.txt").write_text("请回答问题", encoding="utf-8")
            (output / "capture_session_id.txt").write_text("stream-test\n", encoding="utf-8")
            client = StreamingClient()

            self.assertTrue(service.submit_question(output, client=client))

            self.assertEqual(client.partial_on_disk, "第一句")
            self.assertEqual((output / "answer.txt").read_text().strip(), "第一句\n第二句")
            status = json.loads((output / "question_status.json").read_text())
            self.assertEqual(status["generation"]["first_visible_ms"], 1200)

    def test_voice_prompt_requires_short_spoken_answer(self):
        prompt = (ROOT / "voice_question_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("面试现场可以直接说出口", prompt)
        self.assertIn("八十到一百八十个汉字", prompt)
        self.assertIn("不要说“根据知识库”", prompt)

    def test_submitting_question_writes_answer_and_history(self):
        service = load_audio_service()

        class StubClient:
            def ask(self, question, prompt, history=None):
                self.question = question
                self.prompt = prompt
                self.history = history
                return "最终答案"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir()
            (output / "interviewer.txt").write_text("请解释进程和线程", encoding="utf-8")
            (output / "me.txt").write_text("我的回答", encoding="utf-8")
            (output / "interviewer.wav").write_bytes(b"interviewer-audio")
            (output / "me.wav").write_bytes(b"microphone-audio")
            (output / "capture_session_id.txt").write_text("session-123\n", encoding="utf-8")
            client = StubClient()

            self.assertTrue(service.submit_question(output, client=client))
            self.assertEqual((output / "answer.txt").read_text().strip(), "最终答案")
            saved_question = (output / "question.txt").read_text().strip()
            self.assertIn("系统声音识别：\n请解释进程和线程", saved_question)
            self.assertIn("麦克风识别：\n我的回答", saved_question)
            self.assertIn("系统声音识别：\n请解释进程和线程", client.question)
            self.assertIn("麦克风识别：\n我的回答", client.question)
            self.assertEqual(client.history, [])
            self.assertEqual(len(list((output.parent / "questions").rglob("*-answer.txt"))), 1)
            self.assertEqual(len(list((output.parent / "questions").rglob("*-interviewer.wav"))), 1)
            self.assertEqual(len(list((output.parent / "questions").rglob("*-me.wav"))), 1)
            history = (output.parent / "conversation_history.jsonl").read_text(encoding="utf-8")
            self.assertIn("最终答案", history)
            archived_before = set((output.parent / "questions").rglob("*"))
            service.archive_pending_capture(output)
            self.assertEqual(set((output.parent / "questions").rglob("*")), archived_before)

    def test_voice_question_uses_and_audits_knowledge_without_changing_saved_question(self):
        service = load_audio_service()
        from lanshot_common.knowledge import KnowledgeHit, KnowledgeSearchResult

        class StubClient:
            def ask(self, question, prompt, history=None):
                self.question = question
                return "结合个人项目的答案"

        class StubKnowledge:
            def search(self, query):
                self.query = query
                return KnowledgeSearchResult(
                    status="hit",
                    query=query,
                    hits=(KnowledgeHit("负责订单系统重构", 0.93, "项目经历.md"),),
                    elapsed_ms=82,
                    provider_ms=65,
                    request_id="request-test",
                )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir()
            (output / "interviewer.txt").write_text("请介绍项目难点", encoding="utf-8")
            (output / "me.txt").write_text("重点讲订单系统", encoding="utf-8")
            (output / "capture_session_id.txt").write_text("capture-rag\n", encoding="utf-8")
            snapshot = service.prepare_question_snapshot(output)
            client = StubClient()
            knowledge = StubKnowledge()

            self.assertTrue(
                service.submit_snapshot(
                    output,
                    snapshot,
                    client=client,
                    knowledge_service=knowledge,
                )
            )

            saved_question = (output / "question.txt").read_text(encoding="utf-8")
            self.assertNotIn("负责订单系统重构", saved_question)
            self.assertIn("负责订单系统重构", client.question)
            self.assertIn("未经信任", client.question)
            status = json.loads((output / "knowledge_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "hit")
            self.assertEqual(status["hits"][0]["document_name"], "项目经历.md")
            archived = list((output.parent / "questions").rglob("*-knowledge.json"))
            self.assertEqual(len(archived), 1)
            history = [
                json.loads(line)
                for line in (output.parent / "conversation_history.jsonl").read_text().splitlines()
            ]
            self.assertEqual(history[-1]["knowledge"]["hit_count"], 1)

    def test_stop_only_saves_but_submit_stops_and_calls_model(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "interviewer.txt").write_text("系统问题", encoding="utf-8")
            (output / "me.txt").write_text("麦克风补充", encoding="utf-8")
            (output / "answer.txt").write_text("上一轮答案", encoding="utf-8")
            with (
                mock.patch.object(service, "process_id", return_value=123),
                mock.patch.object(service, "stop_capture", return_value=True),
                mock.patch.object(service, "archive_capture") as archive,
                mock.patch.object(service, "submit_question") as submit,
            ):
                self.assertEqual(service.capture_stop(output), 0)
                archive.assert_called_once()
                submit.assert_not_called()
                self.assertEqual((output / "answer.txt").read_text(), "上一轮答案")

            with (
                mock.patch.object(service, "process_id", return_value=123),
                mock.patch.object(service, "stop_capture", return_value=True) as stop_capture,
                mock.patch.object(service, "prepare_question_snapshot") as prepare_snapshot,
                mock.patch.object(service, "submit_snapshot") as submit,
                mock.patch.object(service, "start", return_value=0) as start,
            ):
                snapshot = service.QuestionSnapshot(
                    "session-test",
                    "conversation-test",
                    "session-test",
                    output,
                    "系统问题",
                    "麦克风补充",
                )
                prepare_snapshot.return_value = snapshot
                (output / "voice_capture_command.txt").write_text("submit test\n", encoding="utf-8")
                submissions = service.Queue()
                self.assertEqual(
                    service.capture_submit(output, submission_queue=submissions),
                    0,
                )
                stop_capture.assert_called_once_with(output)
                self.assertIs(submissions.get_nowait(), snapshot)
                submit.assert_not_called()
                start.assert_called_once_with(output, ensure_controller=False)

    def test_overlay_shutdown_stops_audio_and_overlay(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "interviewer.txt").write_text("系统问题", encoding="utf-8")
            (output / "me.txt").write_text("麦克风补充", encoding="utf-8")
            with (
                mock.patch.object(service, "process_id", return_value=123),
                mock.patch.object(service, "stop_capture", return_value=True) as stop_capture,
                mock.patch.object(service, "archive_capture") as archive,
                mock.patch.object(service, "stop_overlay", return_value=True) as stop_overlay,
            ):
                self.assertTrue(service.shutdown_from_overlay(output))
                stop_capture.assert_called_once_with(output)
                archive.assert_called_once()
                stop_overlay.assert_called_once_with(output)

    def test_conversation_sessions_are_persistent_and_isolated(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir()
            first = service.ensure_conversation_session(output)
            service.append_question_history(
                output,
                "第一题",
                "第一答",
                capture_session_id="capture-1",
                conversation_id=first,
            )
            second = service.create_conversation_session(output)["id"]
            service.append_question_history(
                output,
                "第二题",
                "第二答",
                capture_session_id="capture-2",
                conversation_id=second,
            )

            self.assertNotEqual(first, second)
            self.assertEqual(service.current_conversation_id(output), second)
            self.assertEqual(
                [item["answer"] for item in service.load_question_history(
                    output,
                    conversation_id=second,
                )],
                ["第二答"],
            )
            self.assertEqual(
                len((output.parent / "sessions.jsonl").read_text(encoding="utf-8").splitlines()),
                2,
            )
            service.activate_conversation_session(output, first)
            self.assertEqual(service.current_conversation_id(output), first)
            self.assertEqual(
                [item["answer"] for item in service.load_question_history(
                    output,
                    conversation_id=first,
                )],
                ["第一答"],
            )

    def test_voice_mode_switch_spawns_unified_screenshot_controller(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir()
            with mock.patch.object(service.subprocess, "Popen") as launch:
                service.spawn_mode_switch(output, "screenshot")
            command = launch.call_args.args[0]
            self.assertIn("mode_controller.py", command[1])
            self.assertEqual(command[-1], "screenshot")

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
        self.assertIn("type: .screen", source)
        self.assertIn("type: .microphone", source)
        self.assertIn("commonFormat: .pcmFormatInt16", source)
        self.assertIn("sampleRate: 16_000", source)
        self.assertIn("AVAudioConverter(from: inputBuffer.format, to: outputFormat)", source)
        self.assertNotIn("settings: buffer.format.settings", source)
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
