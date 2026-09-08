import io
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sender_service as sender


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class ConfigTests(unittest.TestCase):
    def test_loads_and_normalizes_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "server_url": "http://receiver.local:8787/",
                        "interval_seconds": 60,
                        "poll_timeout_seconds": 10,
                        "include_cursor": True,
                    }
                ),
                encoding="utf-8",
            )

            config = sender.Config.load(path)

            self.assertEqual(config.server_url, "http://receiver.local:8787")
            self.assertEqual(config.interval_seconds, 60)
            self.assertEqual(config.poll_timeout_seconds, 10)
            self.assertTrue(config.include_cursor)

    def test_rejects_non_http_server_url(self):
        with self.assertRaises(sender.ConfigError):
            sender.Config(server_url="file:///tmp/server")


class ScreenshotterTests(unittest.TestCase):
    def test_captures_main_monitor_as_jpeg(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b"\xff\xd8image")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            screenshotter = sender.Screenshotter(
                Path(directory),
                include_cursor=False,
                runner=runner,
            )
            capture_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

            output = screenshotter.capture(capture_id)

            self.assertTrue(output.exists())
            self.assertEqual(output.read_bytes()[:2], b"\xff\xd8")
            self.assertIn("-m", calls[0])
            self.assertIn("-x", calls[0])
            self.assertEqual(calls[0][calls[0].index("-t") + 1], "jpg")
            self.assertNotIn("-C", calls[0])

    def test_reports_capture_failure_without_leaving_empty_file(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "permission denied")

        with tempfile.TemporaryDirectory() as directory:
            screenshotter = sender.Screenshotter(Path(directory), runner=runner)
            with self.assertRaises(sender.CaptureError):
                screenshotter.capture(uuid.uuid4())
            self.assertEqual(list(Path(directory).iterdir()), [])


class NativeRedactorTests(unittest.TestCase):
    def test_replaces_image_when_blocked_words_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            words = root / "words.txt"
            words.write_text("secret\n# ignored\n", encoding="utf-8")
            executable = root / "redactor"
            executable.touch()
            image = root / "capture.jpg"
            image.write_bytes(b"\xff\xd8original")

            def runner(command, **kwargs):
                Path(command[2]).write_bytes(b"\xff\xd8redacted")
                return subprocess.CompletedProcess(command, 0, "", "")

            redactor = sender.NativeRedactor(words, executable=executable, runner=runner)
            redactor.redact(image)

            self.assertEqual(image.read_bytes(), b"\xff\xd8redacted")


class HTTPClientTests(unittest.TestCase):
    def make_config(self):
        return sender.Config(
            server_url="http://receiver.local:8787",
            interval_seconds=0,
            retry_delays=(0.0, 0.0),
        )

    def test_poll_returns_none_for_no_content(self):
        opener = mock.Mock(return_value=FakeResponse(204))
        client = sender.HTTPClient(self.make_config(), opener=opener, sleep=lambda _: None)

        self.assertIsNone(client.poll_task())

    def test_poll_validates_and_returns_task_id(self):
        task_id = "00000000-0000-0000-0000-000000000002"
        response = FakeResponse(200, json.dumps({"id": task_id}).encode())
        opener = mock.Mock(return_value=response)
        client = sender.HTTPClient(self.make_config(), opener=opener, sleep=lambda _: None)

        self.assertEqual(client.poll_task(), uuid.UUID(task_id))
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("/api/v1/agent/next?timeout=25", request.full_url)

    def test_remote_upload_uses_task_endpoint_and_jpeg_content_type(self):
        opener = mock.Mock(return_value=FakeResponse(201, b"{}"))
        client = sender.HTTPClient(self.make_config(), opener=opener, sleep=lambda _: None)
        task_id = uuid.UUID("00000000-0000-0000-0000-000000000003")

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "capture.jpg"
            image.write_bytes(b"\xff\xd8image")
            client.upload_remote(task_id, image)

        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.endswith(f"/api/v1/tasks/{task_id}/image"))
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Content-type"], "image/jpeg")
        self.assertEqual(request.data, b"\xff\xd8image")

    def test_scheduled_upload_sets_capture_id_header(self):
        opener = mock.Mock(return_value=FakeResponse(201, b"{}"))
        client = sender.HTTPClient(self.make_config(), opener=opener, sleep=lambda _: None)
        capture_id = uuid.UUID("00000000-0000-0000-0000-000000000004")

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "capture.jpg"
            image.write_bytes(b"\xff\xd8image")
            client.upload_scheduled(capture_id, image)

        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v1/images"))
        self.assertEqual(request.headers["X-lanshot-capture-id"], str(capture_id))

    def test_retries_server_error_then_succeeds(self):
        server_error = urllib.error.HTTPError(
            "http://receiver.local:8787/api/v1/agent/next",
            500,
            "server error",
            {},
            io.BytesIO(b""),
        )
        opener = mock.Mock(side_effect=[server_error, FakeResponse(204)])
        sleeps = []
        client = sender.HTTPClient(self.make_config(), opener=opener, sleep=sleeps.append)

        self.assertIsNone(client.poll_task())
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(sleeps, [0.0])

    def test_does_not_retry_client_error(self):
        client_error = urllib.error.HTTPError(
            "http://receiver.local:8787/api/v1/agent/next",
            400,
            "bad request",
            {},
            io.BytesIO(b""),
        )
        opener = mock.Mock(side_effect=client_error)
        client = sender.HTTPClient(self.make_config(), opener=opener, sleep=lambda _: None)

        with self.assertRaises(urllib.error.HTTPError):
            client.poll_task()
        self.assertEqual(opener.call_count, 1)


class SenderServiceTests(unittest.TestCase):
    def test_remote_task_captures_then_worker_uploads_durable_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "capture.jpg"
            image.write_bytes(b"\xff\xd8image")
            screenshotter = mock.Mock()
            screenshotter.capture.return_value = image
            client = mock.Mock()
            service = sender.SenderService(
                sender.Config(server_url="http://receiver.local:8787", interval_seconds=0, spool_dir=Path(directory)),
                screenshotter,
                client,
            )
            task_id = uuid.UUID("00000000-0000-0000-0000-000000000005")

            service.handle_remote_task(task_id)

            screenshotter.capture.assert_called_once_with(task_id)
            client.upload_record.assert_not_called()
            self.assertFalse(image.exists())  # bytes are in SQLite, not lost
            self.assertEqual(service.store.get(str(task_id))["payload"], b"\xff\xd8image")
            service._retry_pending_once()
            client.upload_record.assert_called_once()
            self.assertEqual(client.upload_record.call_args.args[0]["id"], str(task_id))
            self.assertEqual(service.store.get(str(task_id))["state"], "delivered")

    def test_f24_unicode_is_recognized(self):
        self.assertTrue(sender.MacF24Listener.is_f24("\uf71b"))
        self.assertTrue(sender.MacF24Listener.is_f24("", 65287))
        self.assertFalse(sender.MacF24Listener.is_f24(""))
        self.assertFalse(sender.MacF24Listener.is_f24("a"))

    def test_f22_unicode_is_recognized(self):
        self.assertTrue(sender.MacF24Listener.is_f22("\uf719"))
        self.assertTrue(sender.MacF24Listener.is_f22("", 65285))
        self.assertFalse(sender.MacF24Listener.is_f22(""))
        self.assertFalse(sender.MacF24Listener.is_f22("a"))

    def test_f23_unicode_is_recognized(self):
        self.assertTrue(sender.MacF24Listener.is_f23("\uf71a"))
        self.assertTrue(sender.MacF24Listener.is_f23("", 65286))
        self.assertFalse(sender.MacF24Listener.is_f23(""))

    def test_voice_controller_sends_navigation_action(self):
        opener = mock.Mock(return_value=FakeResponse(200, b"{}"))
        controller = sender.VoiceController(
            "https://mobile.example/voice/token/", "token-123", opener=opener
        )

        controller.control("next")

        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://mobile.example/voice/token/api/control"
        )
        self.assertEqual(json.loads(request.data), {"action": "next"})
        self.assertEqual(request.headers["X-voice-token"], "token-123")

    def test_f22_writes_page_command(self):
        with tempfile.TemporaryDirectory() as directory:
            page_command = Path(directory) / "page.txt"
            service = sender.SenderService(
                sender.Config(server_url="http://receiver.local:8787", spool_dir=Path(directory)),
                mock.Mock(),
                mock.Mock(),
                page_command_path=page_command,
            )

            service._trigger_page_down()

            command, timestamp = page_command.read_text(encoding="utf-8").split()
            self.assertEqual(command, "down")
            self.assertTrue(timestamp.isdigit())

            service._trigger_page_up()

            command, timestamp = page_command.read_text(encoding="utf-8").split()
            self.assertEqual(command, "up")
            self.assertTrue(timestamp.isdigit())

            service._trigger_move_to_mouse()

            command, timestamp = page_command.read_text(encoding="utf-8").split()
            self.assertEqual(command, "mouse")
            self.assertTrue(timestamp.isdigit())

            service._trigger_toggle_overlay()

            command, timestamp = page_command.read_text(encoding="utf-8").split()
            self.assertEqual(command, "toggle")
            self.assertTrue(timestamp.isdigit())

    def test_service_uses_hotkey_listener_without_schedule_thread(self):
        listener = mock.Mock()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service = sender.SenderService(
            sender.Config(server_url="http://receiver.local:8787", interval_seconds=10, spool_dir=Path(temporary.name)),
            mock.Mock(),
            mock.Mock(),
            hotkey_listener=listener,
        )
        service.stop_event.set()

        service.run()

        self.assertTrue(listener.run.called)
        thread_names = [thread.name for thread in service.threads]
        self.assertIn("hotkeys", thread_names)
        self.assertNotIn("schedule", thread_names)


class LaunchAgentTests(unittest.TestCase):
    def test_builds_launch_agent_for_run_subcommand(self):
        plist = sender.build_launch_agent_plist(
            python_path=Path("/opt/homebrew/bin/python3"),
            script_path=Path("/tmp/sender_service.py"),
            config_path=Path("/tmp/config.json"),
            log_path=Path("/tmp/sender.log"),
        )
        encoded = plistlib.dumps(plist)
        decoded = plistlib.loads(encoded)

        self.assertEqual(decoded["Label"], sender.LAUNCH_AGENT_LABEL)
        self.assertEqual(
            decoded["ProgramArguments"],
            [
                "/opt/homebrew/bin/python3",
                "/tmp/sender_service.py",
                "run",
                "--config",
                "/tmp/config.json",
            ],
        )
        self.assertTrue(decoded["KeepAlive"])
        self.assertTrue(decoded["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
