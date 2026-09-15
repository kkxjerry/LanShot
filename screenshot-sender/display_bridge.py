"""Versioned native-display projection. No Tk, no model calls, no image uploads.

Reads sender task identity first, reconciles results from that task's pinned
receiver, and projects one local view. Native application receipts acknowledge
that view, not human reading and not a different receiver's version number.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from reliability import BUILD, Conflict, InstanceLock, atomic_json, atomic_write, error_info, trace

LABELS = {"awaiting_capture":"已收到请求，等待截图", "pending":"已排队，等待上传",
    "inflight":"正在交付接收服务", "queued":"已接收，等待分析", "processing":"正在分析",
    "complete":"分析完成", "delivered":"已交付，等待确认分析状态", "paused":"本次失败，等待处理",
    "uncertain":"处理结果未知；未切换到其他独立接收端", "failed":"本次截图失败", "expired":"本次任务已过期",
    "cancelled":"本次任务已取消", "unconfigured":"模型未配置"}


class DisplayBridge:
    def __init__(self, router, directory: Path):
        self.router, self.store, self.directory = router, router.store, Path(directory)
        self.stream_id = self.store.origin
        with self.store.tx() as con:
            raw = self.store._meta(con,"display_previous")
            self.previous = json.loads(raw) if raw else None
        self.last_mode_request = ""

    def process_mode_request(self) -> None:
        path = self.directory / "mode_request.json"
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            request_id = str(uuid.UUID(request["id"]))
            if request_id == self.last_mode_request:
                return
            if request.get("mode") != "voice" or not 0 <= time.time() - float(request["at"]) <= 60:
                return
            self.last_mode_request = request_id
            controller = Path(__file__).resolve().parents[1] / "unified/mode_controller.py"
            with (self.directory / "mode-switch.log").open("ab") as stream:
                subprocess.Popen(
                    [sys.executable, str(controller), "voice"],
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except FileNotFoundError:
            return
        except Exception as error:
            trace("DISPLAY_MODE_SWITCH_FAILED", **error_info(error))

    def process_capture_request(self) -> None:
        path = self.directory / "capture_request.json"
        try:
            raw = json.loads(path.read_text())
            task_id = str(uuid.UUID(raw["id"]))
            if not 0 <= time.time() - float(raw["at"]) <= 60:
                return  # Do not replay an old UI click on restart.
            if self.router.local_state is None:
                raise Conflict("local control state unavailable; use configured sender capture entry")
            self.router.local_state.store.create_request(self.router.profile, task_id=task_id)
        except FileNotFoundError:
            return
        except Exception as error:
            trace("DISPLAY_CAPTURE_REQUEST_FAILED",**error_info(error))

    def step(self) -> dict:
        self.process_mode_request()
        self.process_capture_request()
        snap = self.store.snapshot()
        current = snap["current"]
        fresh, backend = False, "none"
        # A user-issued receiver request takes priority before sender has captured it.
        if self.router.local_state is not None:
            pending = self.router.local_state.store.snapshot()["current"]
            if (pending and self.store.get(pending["id"]) is None
                and (not current or pending["created"] >= current["created"])):
                current, fresh = pending, True
        if current and current["state"] != "awaiting_capture":
            row, fresh = self.router.lookup_result(current["id"])
            route = self.router.book.get(current["id"])
            backend = route["backend"] if route else "not_dispatched"
            if row is not None:
                current = row
        try:
            status = json.loads((self.store.path.parent/"sender_status.json").read_text())
            last = status.get("last_capture",{})
            if (last.get("state") == "failed" and self.store.get(last.get("id","")) is None
                and (not current or last.get("at",0) >= current["created"])):
                current = {"id":last["id"],"profile":self.router.profile,"state":"failed",
                           "code":last.get("code","capture_failed"),"created":last["at"],"updated":last["at"]}
                fresh, backend = True, "capture"
        except (OSError,ValueError,KeyError,TypeError):
            pass
        if current and current["profile"] != self.router.profile:
            current = None
        if current:
            # A persisted completed result is authoritative even when the receiver is now offline.
            if current["state"] == "complete":
                self.previous = {"id":current["id"],"profile":current["profile"],"answer":current["answer"]}
            text = f"当前任务：{current['id']}\n{LABELS.get(current['state'],current['state'])}"
            if current.get("code"):
                text += f"\n错误码：{current['code']}"
            if not fresh and backend != "not_dispatched":
                text += "\n接收端状态未实时确认；显示已保存的任务状态。"
            if current["state"] == "complete":
                text += "\n\n" + current["answer"]
            elif self.previous and self.previous["id"] != current["id"] and self.previous["profile"] == self.router.profile:
                text += f"\n\n上一任务结果（不是本次答案）：{self.previous['id']}\n{self.previous['answer']}"
            public = {k:current.get(k) for k in ("id","profile","state","code","created","updated")}
        else:
            public, text = None, "当前模式尚无任务。"
        content = {"schema":1,"build":BUILD,"stream_id":self.stream_id,"profile":self.router.profile,
                   "current":public,"text":text,"backend":backend}
        fingerprint = hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.store.tx() as con:
            version = int(self.store._meta(con,"display_version","0"))
            if fingerprint != self.store._meta(con,"display_fingerprint"):
                version += 1
                self.store._set(con,"display_version",version)
                self.store._set(con,"display_fingerprint",fingerprint)
            self.store._set(con,"display_previous",json.dumps(self.previous,ensure_ascii=False))
        view = {**content,"version":version,"at":time.time()}
        atomic_write(self.directory/"latest.txt",(text+"\n").encode())
        speech = current.get("answer","") if current and current["state"] == "complete" else ""
        if speech:
            from receiver_service import extract_speech_text
            speech = extract_speech_text(speech)
        atomic_write(self.directory/"latest_tts.txt",(speech+"\n").encode())
        atomic_json(self.directory/"latest_state.json",view)
        self.consume_receipt(view)
        return view

    def consume_receipt(self, view: dict) -> bool:
        try:
            ack = json.loads((self.directory/"display_ack.json").read_text())
            row = view.get("current")
            if not row or row["state"] != "complete":
                return False
            if (ack.get("stream_id") != self.stream_id or ack.get("task_id") != row["id"]
                    or ack.get("version") != view["version"] or ack.get("consumer") != "native-overlay"):
                return False
            with self.store.tx() as con:
                self.store._set(con,"native_display_receipt",json.dumps(ack,sort_keys=True))
            return True
        except (OSError,ValueError,TypeError):
            return False


def run(settings: Path) -> int:
    from manage_services import read_settings
    from sender_service import Config
    from multi_receiver import build_client
    data = read_settings(settings)
    config = Config.load(Path(data["sender_config"]))
    if not config.routes_file or not config.display_dir:
        raise ValueError("native display requires routes_file and display_dir; upgrade settings first")
    stop = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *args: stop.set())
    with InstanceLock(config.display_dir/"bridge.lock"):
        router = build_client(config,start_embedded=False)
        bridge = DisplayBridge(router,config.display_dir)
        try:
            while not stop.is_set():
                try:
                    view = bridge.step()
                    atomic_json(config.display_dir/"display_status.json",{"status":"running","at":time.time(),"version":view["version"],"build":BUILD})
                except Exception as error:
                    trace("DISPLAY_BRIDGE_FAILED",**error_info(error))
                    atomic_json(config.display_dir/"display_status.json",{"status":"degraded","at":time.time(),"build":BUILD,**error_info(error)})
                stop.wait(.75)
        finally:
            router.close()
            atomic_json(config.display_dir/"display_status.json",{"status":"stopped","at":time.time(),"build":BUILD})
    return 0


def main():
    from manage_services import DEFAULT_SETTINGS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings",type=Path,default=DEFAULT_SETTINGS)
    return run(parser.parse_args().settings)

if __name__ == "__main__":
    raise SystemExit(main())
