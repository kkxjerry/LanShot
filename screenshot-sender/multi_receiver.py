"""Priority failover with durable route pinning, not parallel model submission.

Local replicas share one receiver DB/cluster. Independent remote receivers do not.
After a possibly delivered request, retries are confined to that cluster. A 404
from a *different* cluster is never evidence that the original did not execute.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from reliability import (BUILD, PROTOCOL, Conflict, LeaseLost, TaskStore, atomic_json,
                         error_info, trace)

MAX_REPLY = 2 * 1024 * 1024
PUBLIC_FIELDS = ("id", "origin", "origin_seq", "profile", "created", "updated", "expires",
                 "digest", "state", "answer", "code")


class NoReceiverAvailable(ConnectionError):
    pass


class OutcomeUnknown(ConnectionError):
    pass


class EndpointMismatch(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EndpointMismatch("receiver redirect refused; credentials/payload must stay at the configured endpoint")


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str  # embedded, local, remote
    url: str = ""
    token_env: str = "LANSHOT_LOCAL_TOKEN"
    expected_cluster: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", self.name):
            raise ValueError("invalid endpoint name")
        if self.kind not in ("embedded", "local", "remote"):
            raise ValueError("invalid endpoint kind")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", self.token_env):
            raise ValueError("token_env must be an environment variable name, never a token value")
        if self.expected_cluster:
            uuid.UUID(self.expected_cluster)
        if self.kind == "embedded":
            if self.url:
                raise ValueError("embedded endpoint must not specify URL")
            return
        u = urllib.parse.urlsplit(self.url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("invalid receiver URL")
        if self.kind == "local" and u.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("local endpoint must use loopback")
        if self.kind == "remote" and u.scheme != "https":
            raise ValueError("remote screenshots require HTTPS")
        if self.kind == "remote" and self.token_env == "LANSHOT_LOCAL_TOKEN":
            raise ValueError("use a separate remote token; do not forward the local token")
        object.__setattr__(self, "url", self.url.rstrip("/"))


class RouteBook:
    """Stored beside sender jobs, with the sender lease fencing every route change."""
    def __init__(self, store: TaskStore):
        self.store = store
        with store.tx() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS receiver_routes (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, profile TEXT NOT NULL,
                backend TEXT NOT NULL, cluster_id TEXT NOT NULL, state TEXT NOT NULL,
                updated REAL NOT NULL, cached TEXT NOT NULL DEFAULT '')""")

    def get(self, task_id: str) -> dict | None:
        with contextlib.closing(self.store._connect()) as con:
            row = con.execute("SELECT * FROM receiver_routes WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def pin(self, job: dict, endpoint: Endpoint, cluster: str) -> None:
        with self.store.tx() as con:
            self.store._owned(con, job)
            old = con.execute("SELECT * FROM receiver_routes WHERE id=?", (job["id"],)).fetchone()
            if old and (old["cluster_id"] != cluster or old["digest"] != job["digest"] or old["profile"] != job["profile"]):
                raise Conflict("task already pinned to another receiver cluster")
            if not old:
                con.execute("INSERT INTO receiver_routes(id,digest,profile,backend,cluster_id,state,updated) VALUES (?,?,?,?,?,'sending',?)",
                    (job["id"], job["digest"], job["profile"], endpoint.name, cluster, time.time()))
            else:
                con.execute("UPDATE receiver_routes SET backend=?,updated=? WHERE id=?", (endpoint.name,time.time(),job["id"]))

    def unpin_refused(self, job: dict) -> None:
        # ONLY after a provably pre-connect failure, never after timeout/5xx/invalid ACK.
        with self.store.tx() as con:
            self.store._owned(con, job)
            con.execute("DELETE FROM receiver_routes WHERE id=? AND state='sending'", (job["id"],))

    def accepted(self, job: dict, endpoint: Endpoint, cluster: str) -> None:
        with self.store.tx() as con:
            self.store._owned(con, job)
            changed = con.execute("UPDATE receiver_routes SET state='accepted',backend=?,updated=? WHERE id=? AND cluster_id=? AND digest=?",
                (endpoint.name,time.time(),job["id"],cluster,job["digest"])).rowcount
            if not changed:
                raise Conflict("no matching durable route")

    def cache(self, task_id: str, cluster: str, row: dict) -> None:
        with self.store.tx() as con:
            route = con.execute("SELECT * FROM receiver_routes WHERE id=?", (task_id,)).fetchone()
            if not route or route["cluster_id"] != cluster or row["digest"] != route["digest"] or row["profile"] != route["profile"]:
                raise Conflict("result does not match routed task")
            con.execute("UPDATE receiver_routes SET cached=?,updated=? WHERE id=?",
                (json.dumps(row,ensure_ascii=False), time.time(), task_id))

    def summary(self) -> list[dict]:
        with contextlib.closing(self.store._connect()) as con:
            return [dict(x) for x in con.execute("SELECT id,backend,cluster_id,state,updated FROM receiver_routes ORDER BY updated DESC LIMIT 100")]


class HTTPBackend:
    def __init__(self, endpoint: Endpoint, *, timeout: float = 10, opener=None):
        self.endpoint, self.timeout = endpoint, timeout
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, *, data: bytes | None = None, headers: dict | None = None,
                timeout: float | None = None) -> tuple[int, dict]:
        token = os.environ.get(self.endpoint.token_env, "")
        if self.endpoint.kind == "remote" and not token:
            raise EndpointMismatch("remote receiver token is not configured")
        outgoing = dict(headers or {})
        if token:
            outgoing["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.endpoint.url + path, data=data,
                                        method="POST" if data is not None else "GET", headers=outgoing)
        with self.opener.open(request, timeout=timeout or self.timeout) as response:
            body = response.read(MAX_REPLY + 1)
            status = response.status
        if len(body) > MAX_REPLY:
            raise EndpointMismatch("oversized receiver response")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise EndpointMismatch("non-object receiver response")
        return status, result

    def health(self) -> dict:
        return self.request("/api/health", timeout=2)[1]

    def submit(self, job: dict) -> dict:
        # All routed jobs use the same idempotent upload entry. Remote request IDs
        # are already durable in local DB; a remote disaster receiver can accept the same ID.
        _, ack = self.request("/api/v1/images", data=job["payload"], headers={
            "Content-Type": "image/jpeg", "X-LanShot-Capture-ID": job["id"],
            "X-LanShot-Origin": job["origin"], "X-LanShot-Sequence": str(job["origin_seq"]),
            "X-LanShot-Profile": job["profile"], "X-LanShot-Created": str(job["created"])})
        return ack

    def lookup(self, task_id: str) -> dict | None:
        try:
            return self.request(f"/api/v1/captures/{task_id}", timeout=2)[1]
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 404:
                return None
            raise


class EmbeddedBackend:
    def __init__(self, endpoint: Endpoint, runtime):
        self.endpoint, self.runtime = endpoint, runtime

    def health(self) -> dict:
        return self.runtime.health()

    def submit(self, job: dict) -> dict:
        if isinstance(self.runtime, ReadOnlyRuntime):
            raise Conflict("display reader must never submit an image")
        row, new = self.runtime.accept_image(job["id"], job["payload"], origin=job["origin"],
            sequence=job["origin_seq"], profile=job["profile"], remote=False, created=job["created"])
        return {"service":"lanshot-receiver", "protocol":PROTOCOL, "capture_id":job["id"],
            "sha256":row["digest"],"durable":True,"cluster_id":self.runtime.cluster_id,
            "status":"accepted" if new else "duplicate","state":row["state"]}

    def lookup(self, task_id: str) -> dict | None:
        row = self.runtime.state.store.get(task_id)
        if row is None:
            return None
        return {"service":"lanshot-receiver","protocol":PROTOCOL,"cluster_id":self.runtime.cluster_id,
                "durable":True,"task":{k:row[k] for k in PUBLIC_FIELDS}}


def refused_before_send(error: BaseException) -> bool:
    cause = getattr(error, "reason", error)
    return isinstance(cause, (ConnectionRefusedError, socket.gaierror)) or (
        isinstance(cause, OSError) and cause.errno in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH))


class MultiReceiverClient:
    def __init__(self, store: TaskStore, backends: list, *, profile: str, prompt_sha256: str,
                 allow_remote: bool = False, local_state=None, runtimes: list | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if not backends or len(backends) > 8 or len({b.endpoint.name for b in backends}) != len(backends):
            raise ValueError("configure between one and eight unique receiver endpoints")
        self.store, self.book = store, RouteBook(store)
        self.backends, self.profile, self.prompt_sha256 = backends, profile, prompt_sha256
        self.allow_remote, self.local_state = allow_remote, local_state
        self.runtimes, self.clock = runtimes or [], clock
        policy = {"profile":profile, "prompt_sha256":prompt_sha256, "allow_remote":allow_remote,
                  "endpoints":[vars(b.endpoint) for b in backends]}
        policy_id = hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()
        self.queue_target = "receiver-policy:" + policy_id
        self.breakers: dict[str, dict] = {}
        self.guard = threading.RLock()
        # Bounds include health+lookup for each route and a single attempted POST.
        self.operation_budget = sum(getattr(b,"timeout",5) + 5 for b in backends) + 30

    def _failure(self, backend, error: BaseException) -> None:
        with self.guard:
            entry = self.breakers.setdefault(backend.endpoint.name, {"failures":0,"until":0})
            entry["failures"] += 1
            if entry["failures"] >= 2:
                entry["until"] = self.clock() + 10
        trace("RECEIVER_ROUTE_FAILED", backend=backend.endpoint.name, **error_info(error))

    def _success(self, backend) -> None:
        with self.guard:
            self.breakers[backend.endpoint.name] = {"failures":0,"until":0}

    def _probe(self, backend, pinned: dict | None = None, *, read_result: bool = False) -> dict:
        endpoint = backend.endpoint
        if endpoint.kind == "remote" and not self.allow_remote:
            raise EndpointMismatch("remote image transmission is disabled")
        with self.guard:
            if self.breakers.get(endpoint.name, {}).get("until",0) > self.clock():
                raise NoReceiverAvailable("receiver circuit open")
        health = backend.health()
        if (health.get("service") != "lanshot-receiver" or health.get("protocol") != PROTOCOL
                or health.get("route_protocol") != 1 or health.get("profile") != self.profile
                or health.get("prompt_sha256") != self.prompt_sha256
                or health.get("status") != "ready" or health.get("storage") != "ready"
                or (health.get("analyzer") != "configured_not_verified"
                    and not (read_result and health.get("read_only") is True))):
            raise EndpointMismatch("receiver identity, prompt, profile, or readiness mismatch")
        try:
            cluster = str(uuid.UUID(health["cluster_id"]))
        except (ValueError,KeyError,TypeError) as error:
            raise EndpointMismatch("missing durable cluster identity") from error
        if endpoint.expected_cluster and endpoint.expected_cluster != cluster:
            raise EndpointMismatch("receiver data store identity changed")
        if pinned and cluster != pinned["cluster_id"]:
            raise EndpointMismatch("task is pinned to another receiver cluster")
        return health

    @staticmethod
    def _verify_ack(ack: dict, job: dict, cluster: str) -> None:
        if (ack.get("service") != "lanshot-receiver" or ack.get("protocol") != PROTOCOL
            or ack.get("durable") is not True or ack.get("capture_id") != job["id"]
            or ack.get("sha256") != job["digest"] or ack.get("cluster_id") != cluster):
            raise EndpointMismatch("invalid durable receiver acknowledgement")

    @staticmethod
    def _verify_result(result: dict, job: dict, cluster: str) -> dict:
        row = result.get("task", {})
        if (result.get("service") != "lanshot-receiver" or result.get("protocol") != PROTOCOL
            or result.get("durable") is not True or result.get("cluster_id") != cluster
            or row.get("id") != job["id"] or row.get("digest") != job["digest"]
            or row.get("profile") != job["profile"] or row.get("origin") != job["origin"]
            or row.get("origin_seq") != job["origin_seq"]):
            raise EndpointMismatch("result does not match the original task identity")
        return row

    def upload_record(self, job: dict) -> dict:
        pinned = self.book.get(job["id"])
        for backend in self.backends:
            # Independent clusters are never queried as a substitute for the owner.
            if pinned and backend.endpoint.expected_cluster and backend.endpoint.expected_cluster != pinned["cluster_id"]:
                continue
            try:
                health = self._probe(backend, pinned)
            except Exception as error:
                self._failure(backend,error)
                continue  # No POST has happened on this path.
            cluster = health["cluster_id"]
            if pinned:
                try:
                    found = backend.lookup(job["id"])
                    if found is not None:
                        self._verify_result(found,job,cluster)
                        self.book.accepted(job,backend.endpoint,cluster)
                        self._success(backend)
                        return {"durable":True,"capture_id":job["id"],"reconciled":True,"cluster_id":cluster}
                except Exception as error:
                    self._failure(backend,error)
                    continue
            self.book.pin(job,backend.endpoint,cluster)  # Commit BEFORE sending any bytes.
            prior_pin = pinned is not None
            try:
                ack = backend.submit(job)
                self._verify_ack(ack,job,cluster)
            except Exception as error:
                if isinstance(error, LeaseLost):
                    raise
                self._failure(backend,error)
                if not prior_pin and refused_before_send(error):
                    self.book.unpin_refused(job)
                    pinned = None
                    continue
                # From now on, even a timeout/5xx/invalid ACK may mean accepted.
                pinned = self.book.get(job["id"])
                continue
            self.book.accepted(job,backend.endpoint,cluster)
            self._success(backend)
            trace("RECEIVER_ROUTE_ACCEPTED",job["id"],backend=backend.endpoint.name,cluster_id=cluster)
            return ack
        if self.book.get(job["id"]):
            raise OutcomeUnknown("receiver outcome is unconfirmed; task stays pinned to its original cluster")
        raise NoReceiverAvailable("all permitted receivers unavailable before submission")

    def lookup_result(self, task_id: str) -> tuple[dict | None, bool]:
        route, job = self.book.get(task_id), self.store.get(task_id)
        if not route or not job:
            return None, False
        for backend in self.backends:
            if backend.endpoint.expected_cluster and backend.endpoint.expected_cluster != route["cluster_id"]:
                continue
            try:
                health = self._probe(backend,route,read_result=True)
                result = backend.lookup(task_id)
                if result is not None:
                    row = self._verify_result(result,job,health["cluster_id"])
                    self.book.cache(task_id,health["cluster_id"],row)
                    self._success(backend)
                    return row, True
            except Exception as error:
                self._failure(backend,error)
        return (json.loads(route["cached"]) if route.get("cached") else None), False

    def health(self) -> dict:
        for backend in self.backends:
            try:
                health = self._probe(backend)
                self._success(backend)
                return health
            except Exception as error:
                self._failure(backend,error)
        raise NoReceiverAvailable("no permitted receiver is ready")

    def poll_task(self) -> uuid.UUID | None:
        # Manual requests are local control-plane state, not dependent on localhost HTTP.
        if self.local_state is None:
            return None
        task = self.local_state.next_task(1)
        return uuid.UUID(task) if task else None

    def report_failure(self, task_id, code, message) -> None:
        if self.local_state:
            self.local_state.fail_task(str(task_id),code)

    def control_task(self, task_id: str, action: str, *, confirm_uncertain: bool = False) -> dict:
        task_id = str(uuid.UUID(task_id))
        if action not in ("retry", "cancel"):
            raise ValueError("unsupported task control")
        route = self.book.get(task_id)
        if route is None:
            if self.local_state is None or self.local_state.store.get(task_id) is None:
                raise Conflict("task has no known processing owner")
            local = self.local_state.store
            if action == "retry":
                local.retry(task_id,model=True,confirm_uncertain=confirm_uncertain)
            else:
                local.cancel(task_id)
            return {"status":"updated","task_id":task_id}
        for backend in self.backends:
            try:
                self._probe(backend,route,read_result=True)
            except Exception:
                continue
            if isinstance(backend,EmbeddedBackend):
                local = backend.runtime.state.store
                if action == "retry":
                    local.retry(task_id,model=True,confirm_uncertain=confirm_uncertain)
                else:
                    local.cancel(task_id)
                return {"status":"updated","task_id":task_id}
            _, result = backend.request("/api/"+action,data=json.dumps({"id":task_id,"confirm_uncertain":confirm_uncertain}).encode(),
                                        headers={"Content-Type":"application/json"})
            return result
        raise OutcomeUnknown("cannot reach the original task owner; no other cluster was invoked")

    def close(self) -> None:
        for runtime in self.runtimes:
            runtime.close()


def load_routes(path: Path, profile: str) -> dict:
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw,dict) or raw.get("version") != 1 or raw.get("profile") != profile:
        raise ValueError("invalid routes file/profile")
    if not isinstance(raw.get("allow_remote"),bool):
        raise ValueError("allow_remote must be explicit true/false")
    for key in ("receiver_dir","prompt_file"):
        if not Path(raw[key]).is_absolute():
            raise ValueError(f"{key} must be absolute")
    endpoints = [Endpoint(**x) for x in raw["endpoints"]]
    if not 1 <= len(endpoints) <= 8 or len({x.name for x in endpoints}) != len(endpoints):
        raise ValueError("invalid endpoint count or duplicate names")
    raw["parsed_endpoints"] = endpoints
    return raw


def build_client(config, *, start_embedded: bool = True, store: TaskStore | None = None):
    from receiver_service import ReceiverState, AnalysisService, KimiClient, VoicePublisher
    from receiver_runtime import ReceiverRuntime
    spec = load_routes(config.routes_file, config.profile)
    store = store or TaskStore(config.spool_dir / "sender_tasks.sqlite3", max_items=config.queue_max_items,
                              max_bytes=config.queue_max_bytes,ttl=config.queue_ttl_seconds)
    root = Path(spec["receiver_dir"])
    prompt = Path(spec["prompt_file"]).read_text()
    signature = hashlib.sha256(prompt.encode()).hexdigest()
    state = None
    try:
        state = ReceiverState(root/"latest.jpg",root/"latest.txt",root/"history",profile=config.profile)
        state.default_prompt = prompt
        state.store.bind_receiver(config.profile,prompt)
    except (OSError,sqlite3.Error) as error:
        # A corrupt/unwritable receiver DB need not also disable a separate sender queue.
        state = None
        trace("LOCAL_RECEIVER_STORE_UNAVAILABLE",**error_info(error))
    key = os.environ.get("DASHSCOPE_API_KEY","")
    publisher = None
    if config.profile == "written" and key and os.environ.get("LANSHOT_VOICE_URL") and os.environ.get("LANSHOT_VOICE_TOKEN"):
        publisher = VoicePublisher(os.environ["LANSHOT_VOICE_URL"],os.environ["LANSHOT_VOICE_TOKEN"],key)
    analyzer = AnalysisService(state,KimiClient(key),prompt,publisher) if key and state is not None else None
    runtimes, backends = [], []
    for endpoint in spec["parsed_endpoints"]:
        if endpoint.kind == "embedded":
            if state is None:
                backends.append(UnavailableBackend(endpoint))
                continue
            if not start_embedded:
                # Read-only display access need not start another model worker.
                runtime = ReadOnlyRuntime(state,signature)
            else:
                try:
                    runtime = ReceiverRuntime(state,analyzer,node="embedded")
                except (OSError,sqlite3.Error) as error:
                    trace("EMBEDDED_RUNTIME_UNAVAILABLE",**error_info(error))
                    backends.append(UnavailableBackend(endpoint))
                    continue
                runtimes.append(runtime)
            backends.append(EmbeddedBackend(endpoint,runtime))
        else:
            backends.append(HTTPBackend(endpoint,timeout=config.request_timeout_seconds))
    return MultiReceiverClient(store,backends,profile=config.profile,prompt_sha256=signature,
        allow_remote=spec["allow_remote"],local_state=state,runtimes=runtimes)


class ReadOnlyRuntime:
    """Local result inspection without spawning a second runtime or spending model tokens."""
    def __init__(self,state,signature):
        self.state,self.cluster_id,self.signature=state,state.store.origin,signature
    def health(self):
        verified = False
        for node in ("embedded","receiver","receiver_backup"):
            try:
                beat = json.loads(self.state.latest_image.with_name(f"{node}_status.json").read_text())
                if (beat.get("cluster_id") == self.cluster_id and beat.get("status") == "running"
                    and beat.get("model") == "configured_not_verified" and -2 <= time.time()-float(beat["at"]) < 5):
                    verified = True
            except (OSError,ValueError,KeyError,TypeError):
                pass
        return {"service":"lanshot-receiver","protocol":PROTOCOL,"route_protocol":1,
            "profile":self.state.profile,"cluster_id":self.cluster_id,"prompt_sha256":self.signature,
            "status":"ready","storage":"ready", "read_only":True,
            "analyzer":"configured_not_verified" if verified else "not_checked"}
    def accept_image(self,*args,**kwargs):
        raise Conflict("read-only display backend must never accept a task")


class UnavailableBackend:
    def __init__(self,endpoint):
        self.endpoint=endpoint
    def health(self):
        raise NoReceiverAvailable("local receiver storage unavailable")
    def submit(self,job):
        raise NoReceiverAvailable("local receiver storage unavailable")
    def lookup(self,task_id):
        raise NoReceiverAvailable("local receiver storage unavailable")
