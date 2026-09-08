import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reliability import (TaskStore, QueueFull, Conflict, LeaseLost, RetryDecision,
                         retry_decision, InstanceLock, Heartbeat, error_info)

IMAGE = b"\xff\xd8synthetic-test-no-screen\xff\xd9"
TARGET = "http://127.0.0.1:8788"

class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds

class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.store = TaskStore(self.root / 'store.sqlite3', clock=self.clock)
    def enqueue(self, **kwargs):
        return self.store.enqueue(str(uuid.uuid4()), IMAGE, target=TARGET, **kwargs)
    def accept(self, sequence, **kwargs):
        task_id = kwargs.pop('task_id', str(uuid.uuid4()))
        return self.store.accept(task_id, IMAGE, origin='sender-A', origin_seq=sequence,
                                 profile='default', prompt='synthetic', configured=True, **kwargs)[0]

class SenderStoreTests(StoreCase):
    def test_durable_capture_survives_store_recreation(self):
        job = self.enqueue()
        fresh = TaskStore(self.store.path, clock=self.clock)
        self.assertEqual(fresh.get(job['id'])['payload'], IMAGE)
        self.assertEqual(fresh.origin, self.store.origin)
    def test_sequence_monotonic_after_restart(self):
        first = self.enqueue()
        fresh = TaskStore(self.store.path, clock=self.clock)
        second = fresh.enqueue(uuid.uuid4(), IMAGE, target=TARGET)
        self.assertGreater(second['origin_seq'], first['origin_seq'])
    def test_duplicate_id_same_payload_is_idempotent(self):
        job = self.enqueue()
        same = self.store.enqueue(job['id'], IMAGE, target=TARGET)
        self.assertEqual(job['seq'], same['seq'])
    def test_id_reuse_with_other_payload_rejected(self):
        job = self.enqueue()
        with self.assertRaises(Conflict): self.store.enqueue(job['id'], b'other', target=TARGET)
    def test_destination_is_immutable(self):
        job = self.enqueue()
        with self.assertRaises(Conflict): self.store.enqueue(job['id'], IMAGE, target='http://other')
        self.assertIsNone(self.store.claim(target='http://other'))
    def test_profile_isolation(self):
        self.enqueue(profile='written')
        self.assertIsNone(self.store.claim(target=TARGET, profile='default'))
        self.assertIsNotNone(self.store.claim(target=TARGET, profile='written'))
    def test_exclusive_claim_between_threads(self):
        self.enqueue()
        barrier = threading.Barrier(8)
        def claim(_):
            barrier.wait()
            return self.store.claim(target=TARGET)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(x is not None for x in results), 1)
    def test_crashed_upload_lease_can_be_reclaimed(self):
        self.enqueue()
        first = self.store.claim(target=TARGET, lease_seconds=10)
        self.clock.advance(11)
        second = self.store.claim(target=TARGET)
        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['lease'], second['lease'])
        with self.assertRaises(LeaseLost): self.store.delivered(first)
        self.store.delivered(second)
    def test_expired_capture_is_visible_not_delivered(self):
        job = self.enqueue()
        self.clock.advance(901)
        self.assertIsNone(self.store.claim(target=TARGET))
        self.assertEqual(self.store.get(job['id'])['state'], 'expired')
        self.assertIsNone(self.store.get(job['id'])['payload'])
    def test_retry_deadline_and_attempt_count_persist(self):
        self.enqueue()
        job = self.store.claim(target=TARGET)
        self.store.release(job, RetryDecision('retry','network',10))
        fresh = TaskStore(self.store.path, clock=self.clock)
        self.assertIsNone(fresh.claim(target=TARGET))
        self.clock.advance(10)
        again = fresh.claim(target=TARGET)
        self.assertEqual(again['attempts'], 2)
    def test_paused_job_does_not_retry_automatically(self):
        self.enqueue()
        job = self.store.claim(target=TARGET)
        self.store.release(job, RetryDecision('pause','http_401'))
        self.clock.advance(20)
        self.assertIsNone(self.store.claim(target=TARGET))
        self.store.retry(job['id'])
        self.assertIsNotNone(self.store.claim(target=TARGET))
    def test_queue_count_limit(self):
        store = TaskStore(self.root/'small.db', max_items=1, clock=self.clock)
        store.enqueue(uuid.uuid4(), IMAGE, target=TARGET)
        with self.assertRaises(QueueFull): store.enqueue(uuid.uuid4(), IMAGE, target=TARGET)
    def test_queue_byte_limit(self):
        store = TaskStore(self.root/'small.db', max_bytes=len(IMAGE)-1)
        with self.assertRaises(QueueFull): store.enqueue(uuid.uuid4(), IMAGE, target=TARGET)
    def test_delivery_releases_payload(self):
        self.enqueue()
        job = self.store.claim(target=TARGET)
        self.store.delivered(job)
        self.assertIsNone(self.store.get(job['id'])['payload'])
        self.assertEqual(self.store.snapshot()['current']['state'], 'delivered')
    def test_transaction_failure_rolls_back_state_and_bytes(self):
        with mock.patch.object(self.store, '_promote', side_effect=sqlite3.OperationalError('full')):
            with self.assertRaises(sqlite3.OperationalError): self.enqueue()
        self.assertEqual(self.store.snapshot()['counts'], {})
    def test_corrupt_database_not_reset_to_empty(self):
        corrupt = self.root/'bad.db'; corrupt.write_bytes(b'not a database')
        with self.assertRaises(sqlite3.DatabaseError): TaskStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), b'not a database')
    def test_database_owner_only_permissions(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
    def test_cancel_is_explicit_terminal_state(self):
        job = self.enqueue(); self.store.cancel(job['id'])
        self.assertEqual(self.store.get(job['id'])['state'], 'cancelled')
        with self.assertRaises(Conflict): self.store.retry(job['id'])
    def test_cannot_cancel_external_operation_in_flight(self):
        self.enqueue(); job = self.store.claim(target=TARGET)
        with self.assertRaises(Conflict): self.store.cancel(job['id'])
    def test_legacy_pending_import_keeps_id_and_is_repeatable(self):
        directory = self.root/'legacy'; directory.mkdir()
        task_id = str(uuid.uuid4()); (directory/'image.jpg').write_bytes(IMAGE)
        record = {'capture_id':task_id,'kind':'scheduled','image_name':'image.jpg','created_at':self.clock()}
        (directory/f'{task_id}.pending.json').write_text(json.dumps(record))
        self.assertEqual(self.store.migrate_pending(directory,target=TARGET,profile='default')['imported'],1)
        self.assertEqual(self.store.get(task_id)['payload'],IMAGE)
        self.assertEqual(self.store.migrate_pending(directory,target=TARGET,profile='default')['imported'],0)
    def test_legacy_invalid_or_orphan_payload_not_auto_imported(self):
        (self.root/'raw.jpg').write_bytes(IMAGE)
        (self.root/'bad.pending.json').write_text('{')
        (self.root/'bad2.pending.json').write_text(json.dumps({'capture_id':str(uuid.uuid4()),'image_name':'../outside.jpg'}))
        result = self.store.migrate_pending(self.root,target=TARGET,profile='default')
        self.assertEqual(result,{'imported':0,'invalid':2})
        self.assertTrue((self.root/'raw.jpg').exists())

class ReceiverJournalTests(StoreCase):
    def test_duplicate_receipt_is_atomic_and_durable(self):
        first=self.accept(1)
        fresh=TaskStore(self.store.path,clock=self.clock)
        again,new=fresh.accept(first['id'],IMAGE,origin='sender-A',origin_seq=1,profile='default',prompt='different',configured=True)
        self.assertFalse(new); self.assertEqual(again['prompt'],'synthetic')
    def test_restart_does_not_blindly_repeat_inflight_model(self):
        self.accept(1); first=self.store.claim(model=True)
        fresh=TaskStore(self.store.path,clock=self.clock)
        self.assertEqual(fresh.recover_model('default'),1)
        self.assertIsNone(fresh.claim(model=True))
        self.assertEqual(fresh.get(first['id'])['state'],'uncertain')
        with self.assertRaises(LeaseLost): self.store.finish(first,'late answer')
    def test_queued_accepted_job_survives_restart_and_is_safe_to_run(self):
        self.accept(1); fresh=TaskStore(self.store.path,clock=self.clock)
        self.assertEqual(fresh.recover_model('default'),0)
        self.assertIsNotNone(fresh.claim(model=True))
    def test_uncertain_retry_requires_explicit_confirmation(self):
        self.accept(1); job=self.store.claim(model=True); self.store.recover_model('default')
        with self.assertRaises(Conflict): self.store.retry(job['id'],model=True)
        self.store.retry(job['id'],model=True,confirm_uncertain=True)
        self.assertIsNotNone(self.store.claim(model=True))
    def test_old_capture_arrives_late_cannot_become_current(self):
        newer=self.accept(2); older=self.accept(1)
        self.assertEqual(self.store.snapshot()['current']['id'],newer['id'])
        oldjob=self.store.claim(model=True,task_id=older['id']); self.store.finish(oldjob,'old')
        self.assertEqual(self.store.snapshot()['current']['id'],newer['id'])
    def test_source_watermark_survives_restart(self):
        newer=self.accept(10)
        self.store=TaskStore(self.store.path,clock=self.clock)
        self.accept(2)
        self.assertEqual(self.store.snapshot()['current']['id'],newer['id'])
    def test_old_completion_does_not_replace_new_answer(self):
        older=self.accept(1); newer=self.accept(2)
        oldjob=self.store.claim(model=True,task_id=older['id']); newjob=self.store.claim(model=True,task_id=newer['id'])
        self.assertTrue(self.store.finish(newjob,'new'))
        self.assertFalse(self.store.finish(oldjob,'old'))
        self.assertEqual(self.store.snapshot()['current']['answer'],'new')
    def test_failed_new_task_exposes_previous_result_separately(self):
        self.accept(1); first=self.store.claim(model=True); self.store.finish(first,'previous answer')
        self.accept(2); second=self.store.claim(model=True); self.store.release(second,RetryDecision('pause','http_401'),model=True)
        snapshot=self.store.snapshot()
        self.assertEqual(snapshot['current']['state'],'paused')
        self.assertEqual(snapshot['current']['answer'],'')
        self.assertEqual(snapshot['previous']['answer'],'previous answer')
    def test_display_ack_must_match_current_version(self):
        self.accept(1); first=self.store.claim(model=True); self.store.finish(first,'answer')
        version=self.store.snapshot()['version']
        self.assertFalse(self.store.mark_displayed(first['id'],version-1))
        self.assertTrue(self.store.mark_displayed(first['id'],version))
        self.accept(2)
        self.assertFalse(self.store.mark_displayed(first['id'],version))
    def test_request_survives_restart_and_reissues_after_lease(self):
        task=self.store.create_request(); self.assertEqual(self.store.poll_request('default'),task)
        fresh=TaskStore(self.store.path,clock=self.clock)
        self.assertIsNone(fresh.poll_request('default'))
        self.clock.advance(121)
        self.assertEqual(fresh.poll_request('default'),task)
    def test_remote_accept_uses_existing_capacity_slot(self):
        store=TaskStore(self.root/'one.db',max_items=1,clock=self.clock)
        task=store.create_request()
        row,new=store.accept(task,IMAGE,origin='s',origin_seq=1,profile='default',prompt='',configured=True,remote=True)
        self.assertTrue(new); self.assertEqual(row['state'],'queued')
    def test_remote_request_header_id_unknown_rejected(self):
        with self.assertRaises(KeyError): self.accept(1,remote=True)
    def test_explicit_pending_request_not_replaced_by_late_upload(self):
        self.accept(1)
        requested=self.store.create_request()
        self.accept(2)
        self.assertEqual(self.store.snapshot()['current']['id'],requested)

    def test_remote_receipt_updates_source_watermark(self):
        task=self.store.create_request()
        self.store.accept(task,IMAGE,origin='sender-A',origin_seq=20,profile='default',prompt='',configured=True,remote=True)
        self.accept(19)
        self.assertEqual(self.store.snapshot()['current']['id'],task)
    def test_unconfigured_receiver_retains_image_not_fake_complete(self):
        row,new=self.store.accept(str(uuid.uuid4()),IMAGE,origin='s',origin_seq=1,profile='default',prompt='',configured=False)
        self.assertEqual(row['state'],'unconfigured'); self.assertEqual(row['payload'],IMAGE)
        self.assertIsNone(self.store.claim(model=True))
    def test_model_phase_deadline_marks_uncertain_and_fences_late_worker(self):
        self.accept(1); job=self.store.claim(model=True,lease_seconds=10)
        self.clock.advance(11); self.assertEqual(self.store.model_deadlines('default'),1)
        with self.assertRaises(LeaseLost): self.store.finish(job,'too late')
    def test_diagnostic_snapshot_excludes_answers_and_payloads(self):
        self.accept(1); job=self.store.claim(model=True); self.store.finish(job,'secret-answer')
        encoded=json.dumps(self.store.snapshot(include_answers=False))
        self.assertNotIn('secret-answer',encoded); self.assertNotIn('synthetic',encoded)

class RetryPolicyTests(unittest.TestCase):
    def error(self,status,retry_after=None):
        error=urllib.error.HTTPError('http://127.0.0.1',status,'error',{'Retry-After':str(retry_after)} if retry_after else {},io.BytesIO(b'sensitive'))
        self.addCleanup(error.close); return error
    def test_permanent_400_and_401_pause(self):
        for status in (400,401,403,404,409,413): self.assertEqual(retry_decision(self.error(status),1).action,'pause')
    def test_upload_transient_errors_retry(self):
        for status in (408,429,500,502,503,504): self.assertEqual(retry_decision(self.error(status),1).action,'retry')
    def test_model_ambiguous_http_errors_uncertain(self):
        for status in (408,500,502,504): self.assertEqual(retry_decision(self.error(status),1,model=True).action,'uncertain')
    def test_model_transport_timeout_is_uncertain(self):
        self.assertEqual(retry_decision(TimeoutError(),1,model=True).action,'uncertain')
    def test_429_respects_retry_after_and_caps_it(self):
        self.assertEqual(retry_decision(self.error(429,999999),1).delay,600)
    def test_retry_limit_pauses(self):
        self.assertEqual(retry_decision(self.error(503),6).action,'pause')
    def test_error_metadata_does_not_leak_response_or_credentials(self):
        error=self.error(401); info=error_info(error)
        self.assertEqual(info['http_status'],401)
        self.assertNotIn('sensitive',json.dumps(info))

class RuntimePrimitiveTests(StoreCase):
    def test_instance_lock_prevents_double_service(self):
        with InstanceLock(self.root/'instance.lock'):
            with self.assertRaises(Conflict):
                with InstanceLock(self.root/'instance.lock'): pass
        with InstanceLock(self.root/'instance.lock'): pass
    def test_idle_heartbeat_is_not_a_stall(self):
        hb=Heartbeat(self.root/'hb.json','sender'); hb.progress('upload','idle')
        with mock.patch('reliability.time.monotonic',return_value=10**12): self.assertEqual(hb.stalled(),[])
    def test_busy_phase_deadline_detects_stall(self):
        hb=Heartbeat(self.root/'hb.json','sender'); hb.progress('upload','busy',deadline=1)
        with mock.patch('reliability.time.monotonic',return_value=10**12): self.assertEqual(hb.stalled(),['upload'])
