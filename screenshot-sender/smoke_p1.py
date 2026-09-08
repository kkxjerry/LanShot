#!/usr/bin/env python3
"""Safe P1 rehearsal: synthetic bytes + real loopback HTTP + stub model.
No desktop capture, paid model, real launchd, or persistent user state is used.
"""
import json
import tempfile
import threading
import time
from pathlib import Path
from receiver_service import ReceiverState, AnalysisService, create_server
from sender_service import Config, HTTPClient, SenderService


class SyntheticCapture:
    def __init__(self, root): self.root = root
    def capture(self, task_id):
        path = self.root / f'{task_id}.jpg'
        path.write_bytes(b'\xff\xd8synthetic-protocol-test\xff\xd9')
        return path


class StubModel:
    def __init__(self): self.calls = 0
    def analyze(self, image, prompt):
        self.calls += 1
        return 'Synthetic P1 rehearsal succeeded; no actual model was called.'


def main():
    with tempfile.TemporaryDirectory(prefix='lanshot-p1-smoke-') as directory:
        root = Path(directory)
        spool = root / 'sender'; spool.mkdir()
        state = ReceiverState(root / 'receiver' / 'latest.jpg')
        model = StubModel()
        server = create_server('127.0.0.1', 0, state, AnalysisService(state, model, 'synthetic'))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = Config(f'http://127.0.0.1:{server.server_address[1]}', spool_dir=spool)
            client = HTTPClient(config)
            sender = SenderService(config, SyntheticCapture(spool), client)
            task_id = sender.handle_manual_capture()
            original = sender.store.get(task_id)
            assert original['state'] == 'pending'
            sender._retry_pending_once()
            assert sender.store.get(task_id)['state'] == 'delivered'
            deadline = time.monotonic() + 5
            while state.store.get(task_id)['state'] != 'complete':
                if time.monotonic() >= deadline: raise RuntimeError('analysis did not complete')
                time.sleep(0.01)
            duplicate_ack = client.upload_record(original)
            assert duplicate_ack['status'] == 'duplicate'
            assert model.calls == 1
            print(json.dumps({'status':'passed', 'capture':'synthetic', 'transport':'real_loopback_HTTP',
                              'model':'stub_not_paid', 'upload':'delivered', 'analysis':'complete',
                              'duplicate_model_calls':0, 'user_state_modified':False}, indent=2))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
