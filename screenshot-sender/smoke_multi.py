#!/usr/bin/env python3
"""Synthetic embedded -> local-primary -> local-backup rehearsal.

Uses temporary files, loopback HTTP and a stub model. No screen capture, real
provider, launchd, remote host or native application is started.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
from display_bridge import DisplayBridge
from multi_receiver import Endpoint, EmbeddedBackend, HTTPBackend, MultiReceiverClient
from receiver_runtime import ReceiverRuntime
from receiver_service import AnalysisService, ReceiverState, create_server
from reliability import BUILD
from sender_service import Config, SenderService
from smoke_p1 import SyntheticCapture


class CountingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def analyze(self, image: bytes, prompt: str) -> str:
        with self.lock:
            self.calls += 1
        return '合成测试结果；未调用真实模型。'


def wait_for(predicate, seconds: float = 8.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError('synthetic rehearsal timed out')
        time.sleep(.02)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='lanshot-multi-smoke-') as directory:
        root = Path(directory)
        spool = root / 'sender'
        spool.mkdir()
        prompt = 'Synthetic protocol test only.'
        signature = hashlib.sha256(prompt.encode()).hexdigest()
        model = CountingModel()
        runtimes, servers, threads = [], [], []

        def state() -> ReceiverState:
            return ReceiverState(root / 'receiver' / 'latest.jpg')

        shared = state()
        runtime = ReceiverRuntime(shared, AnalysisService(shared, model, prompt), node='embedded')
        runtimes.append(runtime)
        backends = [EmbeddedBackend(Endpoint('embedded', 'embedded', expected_cluster=runtime.cluster_id), runtime)]
        try:
            for name in ('local-primary', 'local-backup'):
                replica = state()
                server = create_server('127.0.0.1', 0, replica, AnalysisService(replica, model, prompt), node=name)
                servers.append(server)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                threads.append(thread)
                endpoint = Endpoint(name, 'local', f'http://127.0.0.1:{server.server_address[1]}',
                                    expected_cluster=runtime.cluster_id)
                backends.append(HTTPBackend(endpoint, timeout=2))
            from reliability import TaskStore
            store = TaskStore(spool / 'sender_tasks.sqlite3')
            router = MultiReceiverClient(store, backends, profile='default', prompt_sha256=signature,
                                         local_state=shared)
            sender = SenderService(Config(backends[1].endpoint.url, spool_dir=spool), SyntheticCapture(spool), router)
            bridge = DisplayBridge(router, root / 'display')
            outcomes = []
            for index, expected in enumerate(('embedded', 'local-primary', 'local-backup')):
                if index == 1:
                    runtime.close()
                if index == 2:
                    servers[0].shutdown()
                    servers[0].server_close()
                task = sender.handle_manual_capture()
                original = sender.store.get(task)
                sender._retry_pending_once()
                if sender.store.get(task)['state'] != 'delivered':
                    raise AssertionError('task was not durably delivered')
                wait_for(lambda: shared.store.get(task)['state'] == 'complete')
                pinned = router.book.get(task)
                if pinned['backend'] != expected:
                    raise AssertionError(f'expected {expected}, got {pinned["backend"]}')
                # A duplicate through the last replica cannot invoke the model again.
                backends[-1].submit(original)
                view = bridge.step()
                if view['current']['id'] != task or view['current']['state'] != 'complete':
                    raise AssertionError('display projection does not match the current task')
                outcomes.append({'selected': expected, 'durable_upload': True, 'result_projection': 'matched'})
            if model.calls != 3:
                raise AssertionError(f'expected three model calls for three tasks; got {model.calls}')
            print(json.dumps({'build': BUILD, 'status': 'passed', 'cases': outcomes,
                'tasks': 3, 'stub_model_calls': model.calls, 'duplicate_model_calls': 0,
                'real_model_calls': 0, 'desktop_capture': False, 'user_state_modified': False,
                'transport': 'direct_function_and_real_loopback_HTTP',
                'native_app_rendered': False, 'remote_host_deployed': False}, ensure_ascii=False, indent=2))
        finally:
            for server in reversed(servers):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)
            for item in runtimes:
                item.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
