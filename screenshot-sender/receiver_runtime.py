"""Shared local receiver runtime, usable in a sender process or HTTP frontends.

Only the OS-lock owner may invoke the model. A standby never recovers an active
owner's jobs. Ownership is retained until the old worker actually exits; model
timeouts/crashes remain uncertain rather than triggering blind duplicate calls.
"""
from __future__ import annotations

import threading
from typing import Any, Callable
from reliability import BUILD, PROTOCOL, Conflict, Heartbeat, InstanceLock, trace, error_info


class ReceiverRuntime:
    def __init__(self, state: Any, analyzer: Any = None, *, node: str = "receiver",
                 on_fault: Callable[[], None] | None = None):
        if not node or not node.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid receiver node name")
        self.state, self.analyzer, self.node = state, analyzer, node
        self.stop_event, self.wake, self.fault = threading.Event(), threading.Event(), threading.Event()
        self.leader = threading.Event()
        self.on_fault = on_fault
        prompt = analyzer.prompt if analyzer is not None else state.default_prompt
        self.prompt_sha256 = state.store.bind_receiver(state.profile, prompt)
        self.cluster_id = state.store.origin
        self.heartbeat = Heartbeat(state.latest_image.with_name(f"{node}_status.json"), "lanshot-receiver")
        self.worker = threading.Thread(target=self._work, name=f"{node}-analysis", daemon=True)
        self.monitor = threading.Thread(target=self._monitor, name=f"{node}-monitor", daemon=True)
        self.state.render()
        self.heartbeat.write("starting", cluster_id=self.cluster_id, node=node)
        self.worker.start()
        self.monitor.start()

    def _work(self) -> None:
        try:
            while not self.stop_event.is_set():
                if self.analyzer is None:
                    self.heartbeat.progress("analysis", "unconfigured")
                    self.stop_event.wait(.25)
                    continue
                owner = InstanceLock(self.state.store.path.with_name("analysis-owner.lock"))
                try:
                    owner.__enter__()
                except Conflict:
                    self.heartbeat.progress("analysis", "standby")
                    self.stop_event.wait(.25)
                    continue
                try:
                    self.leader.set()
                    self.state.store.recover_model(self.state.profile)
                    trace("RECEIVER_OWNER_ACQUIRED", node=self.node, cluster_id=self.cluster_id)
                    while not self.stop_event.is_set():
                        self.heartbeat.progress("analysis", "checking_queue", deadline=15)
                        if not self.analyzer.run_one(self.heartbeat):
                            self.heartbeat.progress("analysis", "idle")
                            self.wake.wait(.25)
                            self.wake.clear()
                finally:
                    self.leader.clear()
                    owner.__exit__(None, None, None)
        except Exception as error:
            self.fault.set()
            trace("WORKER_FAILED", component=self.node, **error_info(error))

    def _monitor(self) -> None:
        cycles = 0
        while not self.stop_event.wait(1):
            try:
                stalled = self.heartbeat.stalled()
                if stalled:
                    if self.leader.is_set():
                        self.state.store.model_deadlines(self.state.profile)
                    self.fault.set()
                self.state.render()
                self.heartbeat.write("failed" if self.fault.is_set() else "running",
                    model="configured_not_verified" if self.analyzer else "unconfigured",
                    leader=self.leader.is_set(), stalled=stalled, node=self.node, cluster_id=self.cluster_id)
                cycles += 1
                if cycles % 60 == 0 and self.leader.is_set():
                    self.state.store.maintenance()
                if self.fault.is_set():
                    self.stop_event.set()
                    if self.on_fault:
                        self.on_fault()
                    return
            except Exception as error:
                trace("RECEIVER_MONITOR_FAILED", node=self.node, **error_info(error))
                self.fault.set()
                self.stop_event.set()
                if self.on_fault:
                    self.on_fault()
                return

    def health(self) -> dict:
        storage = self.state.storage_ready()
        healthy = storage and not self.fault.is_set() and not self.stop_event.is_set() and self.worker.is_alive()
        return {"service": "lanshot-receiver", "build": BUILD, "protocol": PROTOCOL,
            "profile": self.state.profile, "instance": self.heartbeat.instance,
            "cluster_id": self.cluster_id, "prompt_sha256": self.prompt_sha256,
            "route_protocol": 1, "node": self.node,
            "status": "ready" if healthy else "degraded", "storage": "ready" if storage else "failed",
            "worker": "running" if healthy else "failed", "leader": self.leader.is_set(),
            "analyzer": "configured_not_verified" if self.analyzer else "unconfigured"}

    def accept_image(self, capture_id: str, image: bytes, *, origin: str, sequence: int,
                     profile: str, remote: bool, created: float | None = None) -> tuple[dict, bool]:
        if profile != self.state.profile:
            raise Conflict("receiver profile mismatch")
        row, new = self.state.store.accept(capture_id, image, origin=origin, origin_seq=sequence,
            profile=profile, prompt=self.analyzer.prompt if self.analyzer else self.state.default_prompt,
            configured=self.analyzer is not None, remote=remote, created=created)
        trace("UPLOAD_PERSISTED" if new else "UPLOAD_DUPLICATE", capture_id, state=row["state"], node=self.node)
        try:
            self.state.render()
        except Exception as error:
            trace("RESULT_RENDER_FAILED", capture_id, **error_info(error))
        self.wake.set()
        return row, new

    def close(self) -> None:
        self.stop_event.set()
        self.wake.set()
        self.worker.join(timeout=1)
        self.monitor.join(timeout=1)
        # The worker owns its lock. Never release it while a model call is running.
        self.heartbeat.write("stopping" if self.worker.is_alive() else "stopped", node=self.node)
