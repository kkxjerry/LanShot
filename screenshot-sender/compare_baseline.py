#!/usr/bin/env python3
"""Read-only comparison against the uploaded source snapshot. Never applies a patch."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True, help='existing screenshot-sender folder')
    args = parser.parse_args()
    expected = json.loads(Path(__file__).with_name('BASELINE_SHA256.json').read_text())['files']
    changed = []
    for name, digest in expected.items():
        path = args.target / name
        status = 'missing' if not path.is_file() else ('match' if hashlib.sha256(path.read_bytes()).hexdigest() == digest else 'modified')
        if status != 'match':
            changed.append({'file': name, 'status': status})
    print(json.dumps({'read_only': True, 'baseline': 'uploaded_snapshot_not_local_P0',
                      'matches_uploaded_baseline': not changed, 'differences': changed,
                      'safe_to_blindly_overwrite': False}, ensure_ascii=False, indent=2))
    return 2 if changed else 0


if __name__ == '__main__':
    raise SystemExit(main())
