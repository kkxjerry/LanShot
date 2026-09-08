#!/usr/bin/env python3
"""Explicit conservative migration of the P0 receiver ledger; never calls a model.

Stop both receivers first. Completed IDs become tombstones; old processing results
are uncertain. Originals are not deleted or renamed. New tasks are never promoted
from old screenshots because P0 did not persist a trustworthy source sequence.
"""
import argparse
import hashlib
import json
import math
import sys
import uuid
from pathlib import Path
from reliability import TaskStore, QueueFull, InstanceLock, error_info
from manage_services import DEFAULT_SETTINGS, read_settings


def migrate(store: TaskStore, directory: Path, profile: str, prompt: str) -> dict[str, int]:
    path = directory / 'capture_ledger.json'
    if path.is_symlink():
        raise ValueError('symlink ledger refused')
    ledger = json.loads(path.read_text())
    if not isinstance(ledger, dict):
        raise ValueError('invalid P0 ledger; refusing an empty replacement')
    report = {'imported':0,'already_present':0,'invalid_or_blocked':0}
    for raw_id, record in ledger.items():
        try:
            task_id = str(uuid.UUID(raw_id))
            status = record['status']
            if status not in ('complete','processing','failed'):
                raise ValueError('unknown P0 status')
            created = float(record['updated_at'])
            if not math.isfinite(created):
                raise ValueError('invalid timestamp')
            image = directory / 'capture_inbox' / f'{task_id}.jpg'
            if image.is_symlink():
                raise ValueError('symlink image refused')
            payload = image.read_bytes() if image.is_file() else None
            if status != 'complete' and payload is None:
                raise ValueError('P0 item has no image; manual investigation required')
            digest = hashlib.sha256(payload).hexdigest() if payload else 'legacy_digest_unknown'
            state = 'complete' if status == 'complete' else 'uncertain'
            with store.tx() as con:
                if con.execute('SELECT id FROM jobs WHERE id=?',(task_id,)).fetchone():
                    report['already_present'] += 1
                    continue
                if payload:
                    store._capacity(con,len(payload))
                con.execute("INSERT INTO jobs(id,origin,origin_seq,kind,target,profile,created,expires,payload,digest,prompt,state,updated,code) VALUES (?,'p0-migration',0,'scheduled','',?,?,?,?,?,?,?,?,?)",
                            (task_id,profile,created,created+store.ttl,payload,digest,prompt,state,store.clock(),'legacy_result_unverified' if status!='complete' else 'legacy_completed_id'))
            report['imported'] += 1
        except (OSError, ValueError, TypeError, KeyError, QueueFull):
            report['invalid_or_blocked'] += 1
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings',type=Path,default=DEFAULT_SETTINGS)
    parser.add_argument('--legacy-dir',type=Path,required=True)
    parser.add_argument('--confirm-profile',required=True)
    args=parser.parse_args(argv)
    try:
        settings=read_settings(args.settings)
        if args.confirm_profile != settings['profile']:
            raise ValueError('profile confirmation mismatch')
        directory=Path(settings['receiver_dir'])
        with InstanceLock(directory/'receiver.lock'):
            store=TaskStore(directory/'receiver_tasks.sqlite3')
            report=migrate(store,args.legacy_dir,args.confirm_profile,Path(settings['prompt_file']).read_text())
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 2 if report['invalid_or_blocked'] else 0
    except Exception as error:
        print(json.dumps(error_info(error)),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
