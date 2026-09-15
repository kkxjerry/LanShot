import json
import base64
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import receiver_service as receiver
from lanshot_common.knowledge import KnowledgeHit, KnowledgeSearchResult


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class KimiClientTests(unittest.TestCase):
    def test_extracts_only_tts_script_from_structured_answer(self):
        answer = "【标准答案】\n代码内容\n【语音稿】\n逐行口述内容"

        self.assertEqual(receiver.extract_speech_text(answer), "逐行口述内容")
        tagged = '<speech>\n自然讲解\n</speech>\n<code language="Python">pass</code>'
        self.assertEqual(receiver.extract_speech_text(tagged), "自然讲解")
        self.assertEqual(receiver.extract_speech_text("普通答案"), "普通答案")

    def test_sends_image_and_prompt_to_bailian_kimi(self):
        response = {
            "choices": [{"message": {"content": "截图分析结果"}, "finish_reason": "stop"}]
        }
        opener = mock.Mock(return_value=FakeResponse(json.dumps(response).encode()))
        client = receiver.KimiClient("secret-key", opener=opener)
        image = b"\xff\xd8screen\xff\xd9"

        answer = client.analyze(image, "检查这个屏幕")

        self.assertEqual(answer, "截图分析结果")
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "kimi-k2.7-code")
        self.assertTrue(payload["enable_thinking"])
        self.assertEqual(payload["max_tokens"], 6000)
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertEqual(
            content[0]["image_url"]["url"],
            "data:image/jpeg;base64," + base64.b64encode(image).decode(),
        )
        self.assertEqual(content[1], {"type": "text", "text": "检查这个屏幕"})
        self.assertEqual(request.headers["Authorization"], "Bearer secret-key")

    def test_voice_publisher_sends_answer_and_credentials(self):
        opener = mock.Mock(return_value=FakeResponse(b"{}", status=202))
        publisher = receiver.VoicePublisher(
            "https://mobile.example/voice/token/",
            "token-123",
            "sk-secret",
            opener=opener,
        )

        publisher.publish("答案是 A")

        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://mobile.example/voice/token/api/answers",
        )
        self.assertEqual(
            json.loads(request.data),
            {"text": "答案是 A", "segments": ["答案是 A"]},
        )
        self.assertEqual(request.headers["X-voice-token"], "token-123")
        self.assertEqual(request.headers["X-dashscope-key"], "sk-secret")

    def test_splits_tts_script_on_blank_lines(self):
        answer = "<speech>第一句\n\n解释一行。代码一行。\n\n最后一句</speech>"

        self.assertEqual(
            receiver.extract_speech_segments(answer),
            ["第一句", "解释一行。代码一行。", "最后一句"],
        )


class HistoryTests(unittest.TestCase):
    def test_saves_screenshot_and_matching_answer_by_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = receiver.ReceiverState(
                root / "latest.jpg",
                history_dir=root / "history",
            )

            generation = state.begin_analysis(b"\xff\xd8screen\xff\xd9")
            state.complete_analysis(generation, "这次的答案")

            date_directories = list((root / "history").iterdir())
            self.assertEqual(len(date_directories), 1)
            screenshots = list(date_directories[0].glob("*.jpg"))
            answers = list(date_directories[0].glob("*.txt"))
            self.assertEqual(len(screenshots), 1)
            self.assertEqual(len(answers), 1)
            self.assertEqual(screenshots[0].read_bytes(), b"\xff\xd8screen\xff\xd9")
            self.assertEqual(answers[0].read_text(encoding="utf-8"), "这次的答案\n")


class KnowledgeGroundingTests(unittest.TestCase):
    def test_screenshot_grounding_uses_ocr_before_search(self):
        service = mock.Mock()
        service.enabled.return_value = True
        service.search.return_value = KnowledgeSearchResult(status="empty", query="解释分布式锁")
        ocr = mock.Mock()
        ocr.extract.return_value = "解释分布式锁"
        grounder = receiver.ScreenshotKnowledgeGrounder(service, ocr)

        result = grounder.retrieve(b"jpeg")

        self.assertEqual(result.status, "empty")
        ocr.extract.assert_called_once_with(b"jpeg")
        service.search.assert_called_once_with("解释分布式锁")

    def test_analysis_injects_and_audits_retrieved_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = receiver.ReceiverState(
                root / "latest.jpg",
                history_dir=root / "history",
            )
            generation = state.begin_analysis(b"\xff\xd8screen\xff\xd9")
            task_id = state._generations[generation]
            model = mock.Mock()
            model.analyze.return_value = "结合知识库的答案"
            grounder = mock.Mock()
            grounder.retrieve.return_value = KnowledgeSearchResult(
                status="hit",
                query="介绍订单系统",
                hits=(KnowledgeHit("订单系统采用事务消息", 0.88, "项目资料.md"),),
                elapsed_ms=90,
            )
            analyzer = receiver.AnalysisService(
                state,
                model,
                "回答截图中的问题",
                knowledge_grounder=grounder,
            )

            self.assertTrue(analyzer.run_one(mock.Mock()))

            prompt = model.analyze.call_args.args[1]
            self.assertIn("订单系统采用事务消息", prompt)
            self.assertIn("未经信任", prompt)
            latest = json.loads(state.latest_knowledge.read_text(encoding="utf-8"))
            self.assertEqual(latest["task_id"], task_id)
            self.assertEqual(latest["status"], "hit")
            archived = root / "history" / time.strftime("%Y-%m-%d") / f"{task_id}.knowledge.json"
            self.assertTrue(archived.is_file())

    def test_disabled_knowledge_skips_screenshot_ocr(self):
        service = mock.Mock()
        service.enabled.return_value = False
        ocr = mock.Mock()

        result = receiver.ScreenshotKnowledgeGrounder(service, ocr).retrieve(b"jpeg")

        self.assertEqual(result.status, "disabled")
        ocr.extract.assert_not_called()


class ReceiverHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        latest_image = Path(self.temporary.name) / "latest.jpg"
        self.state = receiver.ReceiverState(latest_image)
        self.ai_finished = threading.Event()
        self.ai_client = mock.Mock()
        self.ai_client.analyze.side_effect = self._finish_analysis
        analyzer = receiver.AnalysisService(self.state, self.ai_client, "默认提示词")
        self.server = receiver.create_server("127.0.0.1", 0, self.state, analyzer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def _finish_analysis(self, image, prompt):
        self.ai_finished.set()
        return "AI 已分析这张截图"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers or {},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.headers, response.read()

    def wait_complete(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.state.analysis()
            if state["status"] == "complete" and self.state.latest_answer.is_file():
                text = self.state.latest_answer.read_text(encoding="utf-8")
                if state["answer"] in text and state["current"]["id"] in text:
                    return
            time.sleep(0.01)
        self.fail("analysis did not finish and publish before deadline")

    def test_web_capture_flow_displays_uploaded_image(self):
        status, _, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("立即截图".encode(), page)

        status, _, body = self.request("/api/capture", method="POST", data=b"")
        self.assertEqual(status, 202)
        task_id = json.loads(body)["id"]

        status, _, body = self.request("/api/v1/agent/next?timeout=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], task_id)

        image = b"\xff\xd8new screenshot\xff\xd9"
        status, _, _ = self.request(
            f"/api/v1/tasks/{task_id}/image",
            method="POST",
            data=image,
            headers={"Content-Type": "image/jpeg"},
        )
        self.assertEqual(status, 201)

        self.wait_complete()
        status, _, body = self.request(f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "complete")

        status, headers, body = self.request("/latest.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assertEqual(body, image)

    def test_scheduled_upload_overwrites_latest_image(self):
        first = b"\xff\xd8first\xff\xd9"
        second = b"\xff\xd8second\xff\xd9"

        for image in (first, second):
            status, _, _ = self.request(
                "/api/v1/images",
                method="POST",
                data=image,
                headers={"Content-Type": "image/jpeg"},
            )
            self.assertEqual(status, 201)

        _, _, body = self.request("/latest.jpg")
        self.assertEqual(body, second)
        self.wait_complete()
        _, _, body = self.request("/api/analysis")
        analysis = json.loads(body)
        self.assertEqual(analysis["status"], "complete")
        self.assertEqual(analysis["answer"], "AI 已分析这张截图")
        text = self.state.latest_answer.read_text(encoding="utf-8")
        self.assertIn("AI 已分析这张截图\n", text)
        self.assertIn(analysis["current"]["id"], text)
        self.assertTrue(self.state.latest_state.is_file())
        self.assertTrue(self.state.store.path.is_file())
        self.assertFalse(list(Path(self.temporary.name).glob("*.tmp")))
        self.assertEqual(
            self.state.latest_speech.read_text(encoding="utf-8"),
            "AI 已分析这张截图\n",
        )
        self.ai_client.analyze.assert_called_with(second, "默认提示词")

    def test_stale_analysis_does_not_overwrite_latest_answer_file(self):
        # Isolate direct state calls from the HTTP server's independent analysis worker.
        with tempfile.TemporaryDirectory() as directory:
            state = receiver.ReceiverState(Path(directory) / "latest.jpg")
            first_generation = state.begin_analysis()
            second_generation = state.begin_analysis()
            state.complete_analysis(second_generation, "新答案")
            state.complete_analysis(first_generation, "过期答案")
            self.assertEqual(state.analysis()["answer"], "新答案")
            text = state.latest_answer.read_text(encoding="utf-8")
            self.assertIn("新答案", text)
            self.assertNotIn("过期答案", text)

    def test_rejects_non_jpeg_upload(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "/api/v1/images",
                method="POST",
                data=b"not an image",
                headers={"Content-Type": "image/jpeg"},
            )
        self.assertEqual(caught.exception.code, 400)

    def test_capture_failure_is_visible_to_browser(self):
        _, _, body = self.request("/api/capture", method="POST", data=b"")
        task_id = json.loads(body)["id"]
        self.request("/api/v1/agent/next?timeout=1")

        failure = json.dumps({"code": "capture_failed", "message": "permission denied"}).encode()
        status, _, _ = self.request(
            f"/api/v1/tasks/{task_id}/failure",
            method="POST",
            data=failure,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)

        _, _, body = self.request(f"/api/tasks/{task_id}")
        payload = json.loads(body)
        self.assertEqual(payload["status"], "paused")
        self.assertEqual(payload["message"], "capture_failed")


if __name__ == "__main__":
    unittest.main()
