"""Durable, bounded task queues. No capture, network, or OS-specific side effects.

SQLite holds payload and state in the SAME transaction. A receiver may acknowledge
only after accept() commits. Network/LLM work must never run in a transaction.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
import traceback
import urllib.error
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

BUILD = "lanshot-p1-20260907.1"
PROTOCOL = 2
MAX_IMAGE_BYTES = 25 * 1024 * 1024
TERMINAL = ("delivered", "complete", "cancelled", "expired")


class QueueFull(RuntimeError):
    pass


class Conflict(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_write(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            name = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        # Directory sync is supported on the target macOS/Linux local filesystems.
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode())


def error_info(error: BaseException) -> dict[str, Any]:
    """Codes and stack locations, NOT exception strings, response bodies, or locals."""
    fields: dict[str, Any] = {"error_type": type(error).__name__}
    if isinstance(error, urllib.error.HTTPError):
        fields["http_status"] = error.code
    cause = getattr(error, "reason", error)
    if isinstance(cause, OSError) and cause.errno is not None:
        fields["errno"] = cause.errno
    if error.__traceback__:
        fields["frames"] = [f"{Path(x.filename).name}:{x.lineno}:{x.name}"
                            for x in traceback.extract_tb(error.__traceback__)[-6:]]
    return fields


def trace(event: str, task_id: str | uuid.UUID | None = None, **fields: Any) -> None:
    # Callers pass only defined metadata. Never pass screenshot/answer/request bodies.
    fields.update(event=event, at=time.time(), build=BUILD)
    if task_id is not None:
        fields["task_id"] = str(task_id)
    logging.getLogger("lanshot.trace").info("trace=%s", json.dumps(fields, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class RetryDecision:
    action: str  # retry, pause, uncertain
    code: str
    delay: float = 0


def retry_decision(error: BaseException, attempt: int, *, model: bool = False,
                   max_attempts: int = 6, jitter: Callable[[], float] = random.random) -> RetryDecision:
    status = error.code if isinstance(error, urllib.error.HTTPError) else None
    if status is not None:
        retryable = status in (408, 429, 500, 502, 503, 504)
        # A model request timing out MAY have been executed. Never auto-repeat it.
        if model and status in (408, 500, 502, 504):
            return RetryDecision("uncertain", f"model_http_{status}_outcome_unknown")
        if not retryable:
            return RetryDecision("pause", f"http_{status}")
    elif isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        if model:
            return RetryDecision("uncertain", "model_transport_outcome_unknown")
    else:
        return RetryDecision("pause", "protocol_or_local_error")
    if attempt >= max_attempts:
        return RetryDecision("pause", "retry_limit")
    delay = min(120.0, 2.0 ** min(attempt, 7)) * (0.75 + 0.5 * jitter())
    if isinstance(error, urllib.error.HTTPError):
        raw = (error.headers or {}).get("Retry-After", "")
        try:
            from email.utils import parsedate_to_datetime
            value = float(raw) if raw.replace(".", "", 1).isdigit() else parsedate_to_datetime(raw).timestamp() - time.time()
            if math.isfinite(value):
                delay = max(delay, min(600.0, max(0.0, value)))
        except (TypeError, ValueError, OverflowError, AttributeError):
            pass
    return RetryDecision("retry", f"http_{status}" if status else "transport_unavailable", delay)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 origin TEXT NOT NULL, origin_seq INTEGER NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL, profile TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
 payload BLOB, digest TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 lease TEXT, lease_until REAL NOT NULL DEFAULT 0, updated REAL NOT NULL,
 code TEXT NOT NULL DEFAULT '', answer TEXT NOT NULL DEFAULT '', completed REAL,
 displayed_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, target, profile, next_at, seq);
CREATE TABLE IF NOT EXISTS source_watermarks (origin TEXT PRIMARY KEY, high INTEGER NOT NULL);
"""


class TaskStore:
    def __init__(self, path: Path, *, max_items: int = 100, max_bytes: int = 256 * 1024 * 1024,
                 ttl: float = 900, clock: Callable[[], float] = time.time) -> None:
        if max_items <= 0 or max_bytes <= 0 or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("queue limits and TTL must be positive")
        self.path = Path(path)
        self.max_items, self.max_bytes, self.ttl = max_items, max_bytes, ttl
        self.clock = clock
        private_dir(self.path.parent)
        with contextlib.closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
        self.path.chmod(0o600)
        with self.tx() as con:
            con.execute("INSERT OR IGNORE INTO meta VALUES ('schema','1')")
            if self._meta(con, "schema") != "1":
                raise ValueError("unsupported task DB schema; refusing to reset it")
            con.execute("INSERT OR IGNORE INTO meta VALUES ('origin',?)", (str(uuid.uuid4()),))
            con.execute("INSERT OR IGNORE INTO meta VALUES ('version','0')")
            self.origin = self._meta(con, "origin")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA secure_delete=ON")
        return con

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @staticmethod
    def _meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    @staticmethod
    def _set(con: sqlite3.Connection, key: str, value: Any) -> None:
        con.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _bump(self, con: sqlite3.Connection) -> None:
        self._set(con, "version", int(self._meta(con, "version", "0")) + 1)

    def _changed(self, con: sqlite3.Connection, task_id: str) -> None:
        if self._meta(con, "current") == task_id:
            self._bump(con)

    def _expire(self, con: sqlite3.Connection, now: float) -> None:
        expired = con.execute("SELECT id FROM jobs WHERE expires<=? AND state NOT IN ('complete','delivered','cancelled','expired','processing','inflight')", (now,)).fetchall()
        for row in expired:
            con.execute("UPDATE jobs SET state='expired',payload=NULL,code='ttl_expired',updated=? WHERE id=?", (now, row[0]))
            self._changed(con, row[0])

    def _compact(self, con: sqlite3.Connection) -> None:
        # Retain current/previous screenshots, not an unbounded second image archive.
        con.execute("UPDATE jobs SET payload=NULL WHERE state='complete' AND id NOT IN (?,?)",
                    (self._meta(con, "current"), self._meta(con, "previous")))

    def _capacity(self, con: sqlite3.Connection, size: int, exclude: str = "") -> None:
        self._compact(con)
        count = con.execute("SELECT count(*) FROM jobs WHERE state NOT IN ('complete','delivered','cancelled','expired') AND id!=?", (exclude,)).fetchone()[0]
        total = con.execute("SELECT coalesce(sum(length(payload)),0) FROM jobs WHERE id!=?", (exclude,)).fetchone()[0]
        if count >= self.max_items or total + size > self.max_bytes:
            raise QueueFull("queue capacity reached; task was not accepted")

    def _promote(self, con: sqlite3.Connection, task_id: str, origin: str, sequence: int) -> None:
        previous = con.execute("SELECT high FROM source_watermarks WHERE origin=?", (origin,)).fetchone()
        if previous is not None and sequence <= previous[0]:
            return
        con.execute("INSERT INTO source_watermarks VALUES (?,?) ON CONFLICT(origin) DO UPDATE SET high=excluded.high", (origin, sequence))
        current = con.execute("SELECT state FROM jobs WHERE id=?", (self._meta(con, "current"),)).fetchone()
        if current and current[0] == "awaiting_capture":
            # An explicit receiver request owns the current-task slot until captured.
            # An unrelated late upload must not replace that request on arrival.
            return
        self._set(con, "current", task_id)
        self._bump(con)

    def enqueue(self, task_id: str | uuid.UUID, payload: bytes, *, kind: str = "scheduled",
                target: str, profile: str = "default", created: float | None = None) -> dict[str, Any]:
        """Sender commit. Source sequence is assigned transactionally and survives restart."""
        task_id = str(uuid.UUID(str(task_id)))
        if kind not in ("scheduled", "remote") or not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("invalid capture payload or kind")
        now = self.clock()
        created = now if created is None else created
        digest = hashlib.sha256(payload).hexdigest()
        with self.tx() as con:
            old = con.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            if old:
                if (old["digest"], old["target"], old["profile"]) != (digest, target, profile):
                    raise Conflict("task ID payload or destination conflict")
                return dict(old)
            self._expire(con, now)
            self._capacity(con, len(payload))
            con.execute("INSERT INTO jobs(id,origin,origin_seq,kind,target,profile,created,expires,payload,digest,state,updated) VALUES (?,?,0,?,?,?,?,?,?,?,'pending',?)",
                        (task_id, self.origin, kind, target, profile, created, created + self.ttl, payload, digest, now))
            seq = con.execute("SELECT seq FROM jobs WHERE id=?", (task_id,)).fetchone()[0]
            con.execute("UPDATE jobs SET origin_seq=? WHERE id=?", (seq, task_id))
            self._promote(con, task_id, self.origin, seq)
            return dict(con.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone())

    def create_request(self, profile: str = "default") -> str:
        task_id, now = str(uuid.uuid4()), self.clock()
        with self.tx() as con:
            self._expire(con, now)
            self._capacity(con, 0)
            con.execute("INSERT INTO jobs(id,origin,origin_seq,kind,target,profile,created,expires,digest,state,updated) VALUES (?, 'request',0,'remote','',?,?,?,'','awaiting_capture',?)",
                        (task_id, profile, now, now + self.ttl, now))
            self._set(con, "current", task_id)
            self._bump(con)
        return task_id

    def poll_request(self, profile: str, lease_seconds: float = 120) -> str | None:
        now = self.clock()
        with self.tx() as con:
            self._expire(con, now)
            row = con.execute("SELECT id FROM jobs WHERE state='awaiting_capture' AND profile=? AND lease_until<=? ORDER BY seq LIMIT 1", (profile, now)).fetchone()
            if not row:
                return None
            con.execute("UPDATE jobs SET lease_until=?,updated=? WHERE id=?", (now + lease_seconds, now, row[0]))
            return row[0]

    def accept(self, task_id: str, payload: bytes, *, origin: str, origin_seq: int,
               profile: str, prompt: str, configured: bool, remote: bool = False,
               created: float | None = None, promote: bool = True) -> tuple[dict[str, Any], bool]:
        """Receiver transaction: durable bytes + ID/digest + queued state BEFORE ACK."""
        task_id = str(uuid.UUID(task_id))
        if not origin or len(origin) > 128 or origin_seq < 0 or origin_seq > 2**63 - 1:
            raise ValueError("invalid source ordering")
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("invalid image size")
        now, digest = self.clock(), hashlib.sha256(payload).hexdigest()
        with self.tx() as con:
            self._expire(con, now)
            old = con.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            if old and old["state"] != "awaiting_capture":
                if old["digest"] != digest or old["profile"] != profile:
                    raise Conflict("ID reused with different payload/profile or legacy digest unknown")
                return dict(old), False
            if remote and old is None:
                raise KeyError("unknown remote request")
            if old and old["profile"] != profile:
                raise Conflict("request profile mismatch")
            self._capacity(con, len(payload), exclude=task_id)
            state = "queued" if configured else "unconfigured"
            if old:
                con.execute("UPDATE jobs SET origin=?,origin_seq=?,payload=?,digest=?,prompt=?,state=?,lease_until=0,updated=? WHERE id=?",
                            (origin, origin_seq, payload, digest, prompt, state, now, task_id))
                con.execute("INSERT INTO source_watermarks VALUES (?,?) ON CONFLICT(origin) DO UPDATE SET high=max(high,excluded.high)", (origin, origin_seq))
                # Remote request's original creation order wins, not its retry arrival.
                if self._meta(con, "current") == task_id:
                    self._bump(con)
            else:
                con.execute("INSERT INTO jobs(id,origin,origin_seq,kind,target,profile,created,expires,payload,digest,prompt,state,updated) VALUES (?,?,?,'scheduled','',?,?,?,?,?,?,?,?)",
                            (task_id, origin, origin_seq, profile, now if created is None else created, now + self.ttl, payload, digest, prompt, state, now))
                if promote:
                    self._promote(con, task_id, origin, origin_seq)
            return dict(con.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()), True

    def claim(self, *, target: str = "", profile: str = "default", model: bool = False,
              lease_seconds: float = 60, task_id: str | None = None) -> dict[str, Any] | None:
        now, token = self.clock(), str(uuid.uuid4())
        ready, active = ("queued", "processing") if model else ("pending", "inflight")
        with self.tx() as con:
            if not model:
                con.execute("UPDATE jobs SET state='pending',lease=NULL,code='upload_lease_expired' WHERE state='inflight' AND lease_until<=?", (now,))
            self._expire(con, now)
            row = con.execute("SELECT * FROM jobs WHERE state=? AND target=? AND profile=? AND next_at<=? AND (? IS NULL OR id=?) ORDER BY seq LIMIT 1", (ready, target, profile, now, task_id, task_id)).fetchone()
            if not row:
                return None
            con.execute("UPDATE jobs SET state=?,lease=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?", (active, token, now + lease_seconds, now, row["id"]))
            self._changed(con, row["id"])
            return dict(con.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def _owned(self, con: sqlite3.Connection, job: dict[str, Any]) -> sqlite3.Row:
        row = con.execute("SELECT * FROM jobs WHERE id=? AND lease=? AND state IN ('inflight','processing')", (job["id"], job["lease"])).fetchone()
        if row is None:
            raise LeaseLost("stale worker cannot commit")
        return row

    def delivered(self, job: dict[str, Any]) -> None:
        with self.tx() as con:
            self._owned(con, job)
            con.execute("UPDATE jobs SET state='delivered',payload=NULL,lease=NULL,code='',updated=?,completed=? WHERE id=?", (self.clock(), self.clock(), job["id"]))
            self._changed(con, job["id"])

    def finish(self, job: dict[str, Any], answer: str) -> bool:
        with self.tx() as con:
            self._owned(con, job)
            con.execute("UPDATE jobs SET state='complete',answer=?,lease=NULL,code='',updated=?,completed=? WHERE id=?", (answer.strip(), self.clock(), self.clock(), job["id"]))
            current = self._meta(con, "current") == job["id"]
            if current:
                self._set(con, "previous", job["id"])
                self._bump(con)
            self._compact(con)
            return current

    def release(self, job: dict[str, Any], decision: RetryDecision, *, model: bool = False) -> None:
        state = ("queued" if model else "pending") if decision.action == "retry" else ("uncertain" if decision.action == "uncertain" else "paused")
        with self.tx() as con:
            self._owned(con, job)
            con.execute("UPDATE jobs SET state=?,lease=NULL,next_at=?,code=?,updated=? WHERE id=?", (state, self.clock() + decision.delay, decision.code, self.clock(), job["id"]))
            self._changed(con, job["id"])

    def recover_model(self, profile: str) -> int:
        """Only call AFTER obtaining the receiver's exclusive process lock."""
        with self.tx() as con:
            rows = con.execute("SELECT id FROM jobs WHERE state='processing' AND profile=?", (profile,)).fetchall()
            for row in rows:
                con.execute("UPDATE jobs SET state='uncertain',lease=NULL,code='interrupted_model_outcome_unknown',updated=? WHERE id=?", (self.clock(), row[0]))
                self._changed(con, row[0])
            return len(rows)

    def model_deadlines(self, profile: str) -> int:
        with self.tx() as con:
            rows = con.execute("SELECT id FROM jobs WHERE state='processing' AND lease_until<=? AND profile=?", (self.clock(), profile)).fetchall()
            for row in rows:
                con.execute("UPDATE jobs SET state='uncertain',lease=NULL,code='model_deadline_outcome_unknown',updated=? WHERE id=?", (self.clock(), row[0]))
                self._changed(con, row[0])
            return len(rows)

    def retry(self, task_id: str, *, model: bool = False, confirm_uncertain: bool = False) -> None:
        with self.tx() as con:
            self._expire(con, self.clock())
            row = con.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] == "uncertain" and not confirm_uncertain:
                raise Conflict("confirm_uncertain required; retry may incur another model call")
            if row["state"] not in ("paused", "uncertain", "unconfigured") or row["payload"] is None:
                raise Conflict("task is not retryable; expired/active/completed tasks cannot be retried")
            con.execute("UPDATE jobs SET state=?,attempts=0,next_at=0,lease=NULL,code='',updated=? WHERE id=?", ("queued" if model else "pending", self.clock(), task_id))
            self._changed(con, task_id)

    def cancel(self, task_id: str) -> None:
        with self.tx() as con:
            row = con.execute("SELECT state FROM jobs WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] in ("processing", "inflight", "complete", "delivered"):
                raise Conflict("cannot cancel an active or completed external operation")
            con.execute("UPDATE jobs SET state='cancelled',payload=NULL,code='user_cancelled',updated=? WHERE id=?", (self.clock(), task_id))
            self._changed(con, task_id)

    def fail_request(self, task_id: str, code: str) -> bool:
        with self.tx() as con:
            count = con.execute("UPDATE jobs SET state='paused',code=?,updated=? WHERE id=? AND state='awaiting_capture'", (code, self.clock(), task_id)).rowcount
            if count:
                self._changed(con, task_id)
            return bool(count)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self._connect()) as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (str(task_id),)).fetchone()
            return dict(row) if row else None

    def snapshot(self, *, include_answers: bool = True) -> dict[str, Any]:
        with self.tx() as con:
            self._expire(con, self.clock())
            def public(key: str) -> dict[str, Any] | None:
                row = con.execute("SELECT id,state,created,updated,attempts,code,answer,displayed_version,profile FROM jobs WHERE id=?", (self._meta(con, key),)).fetchone()
                if not row:
                    return None
                record = dict(row)
                if not include_answers:
                    record.pop("answer", None)
                return record
            counts = {r[0]: r[1] for r in con.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
            return {"version": int(self._meta(con, "version", "0")), "current": public("current"),
                    "previous": public("previous"), "counts": counts,
                    "queued_bytes": con.execute("SELECT coalesce(sum(length(payload)),0) FROM jobs WHERE state NOT IN ('complete','delivered','expired','cancelled')").fetchone()[0]}

    def mark_displayed(self, task_id: str, version: int) -> bool:
        with self.tx() as con:
            if self._meta(con, "current") != task_id or int(self._meta(con, "version", "0")) != version:
                return False
            return bool(con.execute("UPDATE jobs SET displayed_version=? WHERE id=? AND state='complete'", (version, task_id)).rowcount)

    def maintenance(self, *, retain_seconds: float = 7 * 86400) -> None:
        """Expire payloads; retain compact ID/digest tombstones for deduplication.

        No unbounded image growth: completed image/answer history has finite retention.
        Dedup tombstones are intentionally NOT automatically evicted during P1.
        """
        with self.tx() as con:
            self._expire(con, self.clock())
            con.execute("UPDATE jobs SET answer='' WHERE state='complete' AND updated<? AND id NOT IN (?,?)", (self.clock() - retain_seconds, self._meta(con, "current"), self._meta(con, "previous")))
            con.execute("UPDATE jobs SET payload=NULL WHERE state IN ('complete','expired','cancelled','delivered') AND updated<?", (self.clock() - retain_seconds,))
        with contextlib.closing(self._connect()) as con:
            con.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def migrate_pending(self, directory: Path, *, target: str, profile: str) -> dict[str, int]:
        """Explicit P0 import. The operator MUST confirm target/profile first.

        Atomic commit precedes marker rename; repeated import is idempotent. Raw
        orphan JPEGs are never imported because redaction completion is unknown.
        """
        report = {"imported": 0, "invalid": 0}
        for path in sorted(Path(directory).glob("*.pending.json")):
            try:
                value = json.loads(path.read_text())
                name = value["image_name"]
                if Path(name).name != name or name in (".", ".."):
                    raise ValueError("unsafe image name")
                image = path.parent / name
                if image.is_symlink() or path.is_symlink():
                    raise ValueError("symlink import refused")
                self.enqueue(value["capture_id"], image.read_bytes(), kind=value["kind"],
                             target=target, profile=profile, created=float(value["created_at"]))
                path.rename(path.with_suffix(path.suffix + ".imported"))
                report["imported"] += 1
            except (OSError, ValueError, KeyError, TypeError, Conflict, QueueFull):
                report["invalid"] += 1
        return report


class InstanceLock:
    """Per-service OS lock, released even after a crash. No PID-based mass killing."""
    def __init__(self, path: Path):
        self.path, self.file = path, None

    def __enter__(self):
        import fcntl
        private_dir(self.path.parent)
        self.file = self.path.open("a+")
        self.path.chmod(0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise Conflict("another service instance owns this state directory") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None


class Heartbeat:
    def __init__(self, path: Path, service: str):
        self.path, self.service = path, service
        self.instance = str(uuid.uuid4())
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.components: dict[str, dict[str, Any]] = {}

    def progress(self, name: str, phase: str, *, deadline: float | None = None):
        with self.lock:
            self.components[name] = {"phase": phase, "at": time.time(),
                                     "monotonic": time.monotonic(), "deadline": deadline}

    def stalled(self) -> list[str]:
        with self.lock:
            return [name for name, v in self.components.items()
                    if v["deadline"] is not None and time.monotonic() - v["monotonic"] > v["deadline"]]

    def write(self, status: str = "running", **fields: Any):
        with self.lock:
            components = {k: {n: v for n, v in x.items() if n != "monotonic"} for k, x in self.components.items()}
        atomic_json(self.path, {"service": self.service, "build": BUILD, "instance": self.instance,
                               "pid": os.getpid(), "at": time.time(), "uptime": time.monotonic() - self.started,
                               "status": status, "components": components, **fields})
