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
        self.assertIn('name="lanshot-voice-f22"', source)
        self.assertIn('page_overlay("up")', source)
        self.assertIn('page_overlay("down")', source)

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
        self.assertIn('"F22 发送问题 | F23/F24 翻页"', source)
        self.assertIn('title: "发送问题（F22）"', source)
        self.assertIn('(contentView as? VoicePanelContentView)?.pageUp()', source)
        self.assertIn('(contentView as? VoicePanelContentView)?.pageDown()', source)
        self.assertIn('title: "退出 LanShot"', source)
        self.assertIn('title: "会话管理..."', source)
        self.assertIn('title: "继续所选会话"', source)
        self.assertIn('title: "导出 TXT"', source)
        self.assertIn('exportSelectedSession', source)
        self.assertIn('title: "截屏模式"', source)
        self.assertIn('title: "面试模式"', source)
        self.assertIn("SessionManagerWindowController", source)
        self.assertIn('"shutdown \\(UUID().uuidString.lowercased())', source)
        self.assertIn("sharingType = .none", source)

    def test_voice_window_size_uses_mode_specific_live_preferences(self):
        source = (
            ROOT.parent / "capture-exclusion-demo/CaptureExclusionDemo.swift"
        ).read_text(encoding="utf-8")
        self.assertIn('static let voiceWidth = "overlay.voice.width"', source)
        self.assertIn('static let voiceHeight = "overlay.voice.height"', source)
        self.assertIn("OverlayPreferences(isVoiceMode: answerMonitor.isVoiceMode)", source)
        self.assertIn("Self.panelSize(preferences: preferences, fitting: visibleFrame)", source)
        self.assertIn("preferences.widthRange.lowerBound", source)
        self.assertNotIn("let panelSize = followLatest", source)

    def test_voice_question_uses_text_only_deepseek_request(self):
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
        self.assertEqual(payload["model"], "deepseek-v4.1-flash")
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "上一题"})
        self.assertEqual(payload["messages"][2], {"role": "assistant", "content": "上一题答案"})
        self.assertEqual(payload["messages"][3], {"role": "user", "content": "什么是 GIL？"})
        self.assertEqual(request.headers["Authorization"], "Bearer secret-key")
        self.assertEqual(opener.call_args.kwargs["timeout"], 30)
        self.assertEqual(client.last_timing["provider"], "bailian_compatible")

    def test_default_question_client_uses_deepseek_without_loading_gemini_key(self):
        service = load_audio_service()
        with (
            mock.patch.object(service, "load_api_key", return_value="bailian-key"),
            mock.patch.object(service, "load_google_api_key") as google_key,
        ):
            client = service.default_question_client()

        self.assertIsInstance(client, service.VoiceQuestionClient)
        self.assertEqual(client.model, "deepseek-v4.1-flash")
        self.assertFalse(client.enable_thinking)
        google_key.assert_not_called()

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

    def test_gemini_question_uses_developer_api_medium_thinking_stream(self):
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
            "MEDIUM",
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
        self.assertIn("让候选人一眼抓住结论，并能自然复述", prompt)
        self.assertIn("第一句或前两句直接回答核心结论", prompt)
        self.assertIn("不要使用 Markdown 标题、列表、表格、加粗", prompt)
        self.assertIn("资料没有支持时，不得根据行业常见做法补成项目事实", prompt)
        self.assertIn("成功率不要只看格式合法", prompt)
        self.assertIn("GQA/MQA", prompt)
        self.assertIn("高压交付、遗留代码或上线风险", prompt)

    def test_google_aim_question_client_instantiation_and_query_extraction(self):
        service = load_audio_service()
        client = service.GoogleAimQuestionClient("http://127.0.0.1:18888")
        self.assertEqual(client.model, "google-ai-mode-warm")
        self.assertEqual(client.base_url, "http://127.0.0.1:18888")

    def test_retrieval_query_prefers_interviewer_and_removes_question_preamble(self):
        service = load_audio_service()

        self.assertEqual(
            service.retrieval_query(
                "这是蓝shot面试测试音频。第二题，codah是自己写的还是参考开源？哪些是你自己的贡献？",
                "我靠！第二题，CODA是自己写的还是参考开源？哪些是你自己的贡献？",
            ),
            "CODA PaiCLI是自己写的还是参考开源？哪些是你自己的贡献？",
        )
        self.assertEqual(
            service.retrieval_query("第8题。", "啊。"),
            "啊",
        )
        self.assertEqual(
            service.retrieval_query(
                "你接入的到底是模型还是接口？",
                "",
                previous_question=(
                    "系统声音识别：\nMCP 和 Function Calling 有什么区别？\n\n"
                    "麦克风识别：\n我的回答"
                ),
            ),
            "上一题：MCP 和 Function Calling 有什么区别？\n"
            "当前追问：你接入的到底是模型还是接口？",
        )
        self.assertEqual(
            service.retrieval_query(
                "为什么要用 DAG，而不是只让模型自己规划？",
                "",
                previous_question="系统声音识别：\n上一题\n\n麦克风识别：\n回答",
            ),
            "为什么要用 DAG，而不是只让模型自己规划？",
        )
        self.assertNotIn(
            "ReAct Plan Team 模式选择",
            service.retrieval_query("什么任务适合单 Agent，什么任务适合多 Agent？", ""),
        )
        self.assertIn(
            "ReAct Plan Team 模式选择",
            service.retrieval_query("CODA 中什么任务适合单 Agent、多 Agent？", ""),
        )
        self.assertIn(
            "Memory Context",
            service.retrieval_query("CODA 的长期记忆具体怎么存？", ""),
        )

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
            def __init__(self):
                self.calls = 0

            def search_candidates(self, query):
                self.calls += 1
                self.query = query
                return KnowledgeSearchResult(
                    status="hit",
                    query=query,
                    hits=(KnowledgeHit("负责订单系统重构", 0.93, "项目经历.md"),),
                    elapsed_ms=82,
                    provider_ms=65,
                    request_id="request-test",
                )

            def search(self, query):
                raise AssertionError("voice path should use exactly one wide candidate search")

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

            self.assertEqual(knowledge.calls, 1)
            self.assertEqual(knowledge.query, "请介绍项目难点")
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

    def test_voice_submission_passes_coverage_checklist_into_generation_prompt(self):
        service = load_audio_service()
        from lanshot_common.knowledge import KnowledgeHit, KnowledgeSearchResult

        class StubClient:
            def ask(self, question, prompt, history=None):
                self.question = question
                self.prompt = prompt
                self.history = history
                return "完整答案"

        class StubKnowledge:
            def search_candidates(self, query):
                return KnowledgeSearchResult(
                    status="hit",
                    query=query,
                    hits=(
                        KnowledgeHit(
                            "Planner 生成 DAG，程序做依赖检查、环检测和调度，短任务保留 ReAct。",
                            0.95,
                            "CODA规划.md",
                        ),
                        KnowledgeHit(
                            "multi_edit 先准备 FileMutation，再逐文件提交，存在 partial success 边界。",
                            0.93,
                            "CODA多文件.md",
                        ),
                        KnowledgeHit(
                            "expected_sha256 是版本前置检查，os.replace 只保证单文件替换，仍有 TOCTOU。",
                            0.92,
                            "CODA版本.md",
                        ),
                    ),
                    request_id="coverage-checklist",
                )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir()
            (output / "interviewer.txt").write_text(
                "你这个 CODA 最大工程难点是什么？多文件修改怎么防止改到一半出错？",
                encoding="utf-8",
            )
            (output / "capture_session_id.txt").write_text("coverage-main-path\n", encoding="utf-8")
            snapshot = service.prepare_question_snapshot(output)
            client = StubClient()

            self.assertTrue(
                service.submit_snapshot(
                    output,
                    snapshot,
                    client=client,
                    knowledge_service=StubKnowledge(),
                )
            )
            self.assertEqual(client.history, [])
            self.assertIn("本轮内部覆盖检查", client.prompt)
            self.assertIn("面试官本轮明确问点", client.prompt)
            self.assertIn("Planner、DAG校验调度与ReAct分工", client.prompt)
            self.assertIn("多文件编辑提交与部分成功边界", client.prompt)
            self.assertIn("文件版本检查与并发边界", client.prompt)
            self.assertNotIn("本轮内部覆盖检查", client.question)

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

    def test_throttled_writer_coalesces_and_flushes(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "answer.txt"
            writer = service.ThrottledWriter(target, min_interval=0.08)
            # First write executes immediately
            writer.write("chunk 1")
            self.assertEqual(target.read_text(encoding="utf-8").strip(), "chunk 1")
            # Rapid second write within 80ms is buffered, not written to disk yet
            writer.write("chunk 1 + 2")
            self.assertEqual(target.read_text(encoding="utf-8").strip(), "chunk 1")
            # Flush guarantees final buffered content is on disk
            writer.flush()
            self.assertEqual(target.read_text(encoding="utf-8").strip(), "chunk 1 + 2")

    def test_get_voice_prompt_caches_content(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            prompt_file = Path(directory) / "test_prompt.txt"
            prompt_file.write_text("prompt content v1", encoding="utf-8")
            self.assertEqual(service.get_voice_prompt(prompt_file), "prompt content v1")
            # Should read from cache when mtime matches
            service._PROMPT_CACHE[str(prompt_file)] = (prompt_file.stat().st_mtime, "cached override")
            self.assertEqual(service.get_voice_prompt(prompt_file), "cached override")

    def test_unsubmitted_transcript_saved_to_history(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio"
            output.mkdir(parents=True, exist_ok=True)
            (output.parent / "current_session.json").write_text(json.dumps({"id": "conv-1"}), encoding="utf-8")
            (output / "capture_session_id.txt").write_text("session-unsub-1", encoding="utf-8")
            (output / "current_conversation_id.txt").write_text("conv-1", encoding="utf-8")
            (output / "interviewer.txt").write_text("面试官最后的反问和再见", encoding="utf-8")
            (output / "me.txt").write_text("好的谢谢面试官", encoding="utf-8")

            # First save should succeed and append to history
            saved = service.save_unsubmitted_transcript_to_history(output)
            self.assertTrue(saved)
            history = service.load_question_history(output, limit=10, conversation_id="conv-1")
            self.assertEqual(len(history), 1)
            self.assertIn("面试官最后的反问和再见", history[0]["input"])
            self.assertIn("未提交模型", history[0]["answer"])

            # Second call with same capture_session_id should deduplicate (not double-add)
            saved_again = service.save_unsubmitted_transcript_to_history(output)
            self.assertFalse(saved_again)
            history = service.load_question_history(output, limit=10, conversation_id="conv-1")
            self.assertEqual(len(history), 1)

            # Empty text should not be saved
            (output / "capture_session_id.txt").write_text("session-empty", encoding="utf-8")
            (output / "interviewer.txt").write_text("", encoding="utf-8")
            (output / "me.txt").write_text("", encoding="utf-8")
            saved_empty = service.save_unsubmitted_transcript_to_history(output)
            self.assertFalse(saved_empty)
            history = service.load_question_history(output, limit=10, conversation_id="conv-1")
            self.assertEqual(len(history), 1)

    def test_timeline_dialogue_interleaving_and_archiving(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            output.mkdir()
            (output / "capture_session_id.txt").write_text("session-tl-1", encoding="utf-8")
            (output / "current_conversation_id.txt").write_text("conv-tl-1", encoding="utf-8")
            (output.parent / "current_session.json").write_text(json.dumps({"id": "conv-tl-1"}), encoding="utf-8")
            (output / "interviewer.txt").write_text("面试官的问题", encoding="utf-8")
            (output / "me.txt").write_text("我的回答", encoding="utf-8")

            # Write out-of-order timeline events
            timeline_lines = [
                json.dumps({"t": 1002.0, "role": "me", "text": "我：主要用了多路检索融合。"}),
                json.dumps({"t": 1001.0, "role": "interviewer", "text": "面试官：你这个 Agent 是怎么检索的？"}),
                json.dumps({"t": 1003.0, "role": "interviewer", "text": "面试官：那 RRF 融合效果如何？"}),
            ]
            (output / "timeline.jsonl").write_text("\n".join(timeline_lines) + "\n", encoding="utf-8")

            # Verify load_timeline_events sorts chronologically
            events = service.load_timeline_events(output / "timeline.jsonl")
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["role"], "interviewer")
            self.assertEqual(events[1]["role"], "me")
            self.assertEqual(events[2]["role"], "interviewer")

            # Verify format_timeline_dialogue interleaves turns
            dialogue = service.format_timeline_dialogue(output / "timeline.jsonl")
            self.assertIn("【面试官】", dialogue)
            self.assertIn("【我】", dialogue)
            lines = dialogue.strip().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("【面试官】"))
            self.assertTrue(lines[1].startswith("【我】"))
            self.assertTrue(lines[2].startswith("【面试官】"))

            # Verify prepare_question_snapshot uses timeline_text
            snapshot = service.prepare_question_snapshot(output)
            self.assertEqual(snapshot.question, dialogue)
            self.assertEqual(snapshot.timeline_text, dialogue)

            # Verify archive_capture copies timeline.jsonl
            archived_timeline = snapshot.archive_directory / f"{snapshot.identifier}-timeline.jsonl"
            self.assertTrue(archived_timeline.is_file())
            self.assertEqual(archived_timeline.read_text(encoding="utf-8"), (output / "timeline.jsonl").read_text(encoding="utf-8"))

    def test_native_audio_capture_has_timeline_and_punctuation(self):
        source = (ROOT / "native_audio_capture.swift").read_text(encoding="utf-8")
        self.assertIn('"semantic_punctuation_enabled": true', source)
        self.assertIn('"max_sentence_silence": 800', source)
        self.assertIn("TimelineWriter", source)
        self.assertIn('appendingPathComponent("timeline.jsonl")', source)
        self.assertIn("timelineWriter?.append", source)

    def test_generate_and_save_reverse_questions(self):
        service = load_audio_service()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "output"
            output.mkdir()
            (output / "capture_session_id.txt").write_text("session-rev-1", encoding="utf-8")
            (output / "current_conversation_id.txt").write_text("conv-rev-1", encoding="utf-8")
            (output.parent / "current_session.json").write_text(json.dumps({"id": "conv-rev-1"}), encoding="utf-8")

            timeline_lines = [
                json.dumps({"t": 1001.0, "role": "interviewer", "text": "你这个 Agent 为什么一定要做 DAG？"}),
                json.dumps({"t": 1002.0, "role": "me", "text": "主要是为了状态管理。"}),
                json.dumps({"t": 1003.0, "role": "interviewer", "text": "那 ReAct 不也可以吗？为什么还要显式 Plan？"}),
                json.dumps({"t": 1004.0, "role": "me", "text": "因为长任务容易跑偏。"}),
                json.dumps({"t": 1005.0, "role": "interviewer", "text": "那线上 Plan 失败了怎么重试？你们线上真的喜欢用 Plan 吗？"}),
            ]
            (output / "timeline.jsonl").write_text("\n".join(timeline_lines) + "\n", encoding="utf-8")

            questions = service.generate_and_save_reverse_questions(output)
            self.assertTrue(len(questions) >= 2)
            self.assertEqual(questions[0]["priority"], "P0")
            self.assertIn("反问候选池", (output / "reverse_questions.txt").read_text(encoding="utf-8"))
            self.assertTrue((output / "reverse_questions.json").is_file())
            self.assertIn("反问", (output / "answer.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


