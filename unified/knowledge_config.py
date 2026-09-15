#!/usr/bin/env python3
"""Configure and probe LanShot's optional Bailian knowledge retrieval."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lanshot_common.knowledge import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    KnowledgeConfig,
    KnowledgeConfigError,
    KnowledgeService,
)


def load_api_key() -> str:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "com.lanshot.bailian",
            "-a",
            "DASHSCOPE_API_KEY",
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    key = result.stdout.strip() if result.returncode == 0 else ""
    if not key:
        raise RuntimeError("未找到百炼 API Key")
    return key


def configure(arguments: argparse.Namespace) -> dict:
    config = KnowledgeConfig(
        workspace_id=arguments.workspace_id,
        agent_id=arguments.agent_id,
        enabled=True,
        timeout_seconds=arguments.timeout,
        max_hits=arguments.max_hits,
        min_score=arguments.min_score,
        max_context_chars=arguments.max_context_chars,
    )
    config.write(arguments.config)
    return {
        "status": "configured",
        "enabled": True,
        "config": str(arguments.config.expanduser()),
        "workspace_id": config.workspace_id,
        "agent_id": config.agent_id,
    }


def set_enabled(path: Path, enabled: bool) -> dict:
    current = KnowledgeConfig.load(path)
    updated = KnowledgeConfig(
        workspace_id=current.workspace_id,
        agent_id=current.agent_id,
        enabled=enabled,
        timeout_seconds=current.timeout_seconds,
        max_hits=current.max_hits,
        min_score=current.min_score,
        max_context_chars=current.max_context_chars,
    )
    updated.write(path)
    return {"status": "configured", "enabled": enabled, "config": str(path.expanduser())}


def status(path: Path) -> dict:
    try:
        config = KnowledgeConfig.load(path)
    except FileNotFoundError:
        return {"status": "not_configured", "enabled": False, "config": str(path.expanduser())}
    return {
        "status": "configured",
        "enabled": config.enabled,
        "config": str(path.expanduser()),
        "workspace_id": config.workspace_id,
        "agent_id": config.agent_id,
        "timeout_seconds": config.timeout_seconds,
        "max_hits": config.max_hits,
        "min_score": config.min_score,
        "max_context_chars": config.max_context_chars,
    }


def probe(path: Path, query: str) -> dict:
    service = KnowledgeService(load_api_key, config_path=path)
    result = service.search(query)
    return {
        **result.summary(),
        "sources": [
            {
                "document_name": hit.document_name,
                "title": hit.title,
                "score": hit.score,
            }
            for hit in result.hits
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--workspace-id", required=True)
    setup.add_argument("--agent-id", required=True)
    setup.add_argument("--timeout", type=float, default=3.0)
    setup.add_argument("--max-hits", type=int, default=3)
    setup.add_argument("--min-score", type=float, default=0.5)
    setup.add_argument("--max-context-chars", type=int, default=9_000)
    commands.add_parser("status")
    commands.add_parser("enable")
    commands.add_parser("disable")
    check = commands.add_parser("probe")
    check.add_argument("--query", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "configure":
            result = configure(arguments)
        elif arguments.command == "status":
            result = status(arguments.config)
        elif arguments.command == "enable":
            result = set_enabled(arguments.config, True)
        elif arguments.command == "disable":
            result = set_enabled(arguments.config, False)
        else:
            result = probe(arguments.config, arguments.query)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") not in ("failed", "not_configured") else 1
    except (KnowledgeConfigError, FileNotFoundError, RuntimeError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
