#!/usr/bin/env python3
"""Explicit HTTPS receiver entry point. Credentials come only from environment."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def receiver_arguments(args: argparse.Namespace) -> list[str]:
    if not os.environ.get('DASHSCOPE_API_KEY', '').strip():
        raise ValueError('DASHSCOPE_API_KEY is required')
    if not os.environ.get('LANSHOT_LOCAL_TOKEN', '').strip():
        raise ValueError('LANSHOT_LOCAL_TOKEN is required for this remote server')
    if not 1024 <= args.port <= 65535:
        raise ValueError('port must be between 1024 and 65535')
    prompt = args.prompt_file.expanduser().read_text(encoding='utf-8')
    if not prompt.strip():
        raise ValueError('prompt file must not be empty')
    for path in (args.tls_cert, args.tls_key):
        if not path.expanduser().is_file():
            raise ValueError('TLS certificate/key file is missing')
    state = args.state_dir.expanduser().resolve()
    return ['--host', args.host, '--port', str(args.port), '--node', 'remote',
        '--profile', args.profile, '--prompt', prompt,
        '--image', str(state/'latest.jpg'), '--answer-file', str(state/'latest.txt'),
        '--history-dir', str(state/'history'), '--public-host', args.public_host,
        '--tls-cert', str(args.tls_cert.expanduser()), '--tls-key', str(args.tls_key.expanduser())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9443)
    parser.add_argument('--public-host', required=True)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--tls-cert', type=Path, required=True)
    parser.add_argument('--tls-key', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        target = receiver_arguments(args)
    except (OSError, ValueError) as error:
        print(f'Remote receiver configuration failed: {type(error).__name__}', file=sys.stderr)
        return 2
    import receiver_service
    return receiver_service.main(target)


if __name__ == '__main__':
    raise SystemExit(main())
