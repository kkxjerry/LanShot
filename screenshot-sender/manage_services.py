#!/usr/bin/env python3
"""Explicit user-level launchd management. Never pkill, never touch P0 services.

Run `configure`, inspect `start --dry-run`, then explicitly `start` on macOS.
Credentials are read from Keychain by the child, not written into launchd plists.
"""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from reliability import BUILD, Conflict, InstanceLock, atomic_json, atomic_write, error_info, private_dir, trace
from sender_service import Config, ConfigError

DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "LanShotP1"
DEFAULT_SETTINGS = DEFAULT_DIR / "settings.json"
ROLES = ("receiver", "sender")
LABELS = {role: f"com.lanshot.p1.{role}" for role in ROLES}
ROOT = Path(__file__).resolve().parent


def read_settings(path: Path) -> dict[str, Any]:
    data = json.loads(path.expanduser().read_text())
    required = {"state_dir", "sender_config", "receiver_dir", "port", "profile", "prompt_file", "enabled"}
    if not isinstance(data, dict) or not required <= data.keys():
        raise ConfigError("incomplete P1 settings; run configure")
    if not isinstance(data["enabled"], bool) or not 1024 <= int(data["port"]) <= 65535:
        raise ConfigError("invalid enabled flag or port")
    for field in ("state_dir", "sender_config", "receiver_dir", "prompt_file"):
        if not Path(data[field]).is_absolute():
            raise ConfigError(f"{field} must be absolute")
    config = Config.load(Path(data["sender_config"]))
    if config.profile != data["profile"] or config.server_url != f"http://127.0.0.1:{data['port']}":
        raise ConfigError("settings and sender destination/profile differ")
    return data


def configure(path: Path, *, state_dir: Path, port: int, profile: str, prompt: Path) -> None:
    path, state_dir, prompt = path.expanduser().resolve(), state_dir.expanduser().resolve(), prompt.expanduser().resolve()
    if path.exists():
        raise Conflict("settings already exist; edit and inspect them explicitly instead of overwriting")
    if not prompt.is_file():
        raise ConfigError("prompt file is missing")
    if not 1024 <= port <= 65535:
        raise ConfigError("port must be between 1024 and 65535")
    private_dir(state_dir)
    sender_config = state_dir / "sender.json"
    if sender_config.exists():
        raise Conflict("sender.json already exists; refusing to replace it")
    Config(f"http://127.0.0.1:{port}", spool_dir=state_dir / "spool", profile=profile,
           blocked_words_file=Path.home() / ".config/lanshot-sender/blocked_words.txt").write(sender_config)
    atomic_json(path, {"state_dir": str(state_dir), "sender_config": str(sender_config),
                       "receiver_dir": str(state_dir / "receiver"), "port": port,
                       "profile": profile, "prompt_file": str(prompt), "enabled": False})


def launch_agent(role: str, settings: Path, state_dir: Path, *, python: Path = Path(sys.executable)) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError("unknown role")
    return {"Label": LABELS[role],
            "ProgramArguments": [str(python.resolve()), str(ROOT / "manage_services.py"), "run-child",
                                 "--role", role, "--settings", str(settings.resolve())],
            "WorkingDirectory": str(ROOT), "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "ExitTimeOut": 10, "ProcessType": "Background", "Umask": 0o077,
            "StandardOutPath": str(state_dir / f"{role}-bootstrap.log"),
            "StandardErrorPath": str(state_dir / f"{role}-bootstrap.log")}


def keychain(account: str, *, service: str = "com.lanshot.bailian",
             runner: Callable[..., Any] = subprocess.run) -> str:
    result = runner(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                    capture_output=True, text=True, timeout=10, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def restart_allowed(path: Path, *, now: float | None = None, limit: int = 5, window: float = 300) -> bool:
    now = time.time() if now is None else now
    try:
        data = json.loads(path.read_text())
        starts = [float(x) for x in data["starts"] if now - window <= float(x) <= now + window]
    except FileNotFoundError:
        starts = []
    except (ValueError, TypeError, KeyError):
        return False  # A corrupt restart budget must not unleash an infinite loop.
    if len(starts) >= limit:
        return False
    atomic_json(path, {"starts": [*starts, now]})
    return True


def run_child(role: str, settings: Path) -> int:
    data = read_settings(settings)
    root = Path(data["state_dir"])
    private_dir(root)
    with InstanceLock(root / f"{role}-supervisor.lock"):
        if not data["enabled"]:
            atomic_json(root / f"{role}-supervisor.json", {"status": "stopped_by_user", "at": time.time(), "build": BUILD})
            return 0
        if not restart_allowed(root / f"{role}-restart.json"):
            atomic_json(root / f"{role}-supervisor.json", {"status": "restart_budget_exhausted", "at": time.time(), "build": BUILD})
            return 0  # SuccessfulExit=false: deliberate stop, not a restart loop.
        handler = RotatingFileHandler(root / f"{role}.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().handlers = [handler]
        logging.getLogger().setLevel(logging.INFO)
        atomic_json(root / f"{role}-supervisor.json", {"status": "running", "at": time.time(), "build": BUILD})
        try:
            os.environ["DASHSCOPE_API_KEY"] = keychain("DASHSCOPE_API_KEY")
            # Optional written-mode voice values are never placed in settings/plists.
            for name in ("LANSHOT_VOICE_URL", "LANSHOT_VOICE_TOKEN", "LANSHOT_LOCAL_TOKEN"):
                value = keychain(name, service="com.lanshot.p1")
                if value:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            if role == "receiver":
                import receiver_service
                receiver_dir = Path(data["receiver_dir"])
                os.environ["LANSHOT_PROMPT"] = Path(data["prompt_file"]).read_text()
                code = receiver_service.main(["--host", "127.0.0.1", "--port", str(data["port"]),
                    "--profile", data["profile"], "--image", str(receiver_dir / "latest.jpg"),
                    "--answer-file", str(receiver_dir / "latest.txt"), "--history-dir", str(receiver_dir / "history")])
            else:
                from sender_service import run_service, SCREEN_CAPTURE_EXECUTABLE
                if not os.access(SCREEN_CAPTURE_EXECUTABLE, os.X_OK):
                    raise ConfigError("capture executable unavailable")
                run_service(Path(data["sender_config"]), written_mode=data["profile"] == "written")
                code = 0
            return code
        except (ConfigError, Conflict, FileNotFoundError, ValueError) as error:
            trace("SERVICE_BLOCKED", component=role, **error_info(error))
            atomic_json(root / f"{role}-supervisor.json", {"status": "configuration_blocked", "error_type": type(error).__name__, "at": time.time(), "build": BUILD})
            return 0
        except Exception as error:
            trace("SERVICE_FAILED", component=role, **error_info(error))
            atomic_json(root / f"{role}-supervisor.json", {"status": "failed", "error_type": type(error).__name__, "at": time.time(), "build": BUILD})
            return 75
        finally:
            handler.close()


def health(data: dict[str, Any]) -> dict[str, Any]:
    headers = {}
    token = os.environ.get("LANSHOT_LOCAL_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(f"http://127.0.0.1:{data['port']}/api/health", headers=headers)
    with urllib.request.urlopen(req, timeout=2) as response:
        result = json.loads(response.read(65536))
    if result.get("service") != "lanshot-receiver" or result.get("build") != BUILD or result.get("profile") != data["profile"]:
        raise Conflict("port belongs to another service/build/profile")
    heartbeat = json.loads((Path(data["receiver_dir"]) / "receiver_status.json").read_text())
    if result.get("instance") != heartbeat.get("instance"):
        raise Conflict("port belongs to another P1 state directory")
    return result


def control(action: str, settings: Path, *, dry_run: bool = False, reset_budget: bool = False,
            runner: Callable[..., Any] = subprocess.run,
            launch_dir: Path | None = None) -> dict[str, Any]:
    data = read_settings(settings)
    state_dir = Path(data["state_dir"])
    launch_dir = launch_dir or Path.home() / "Library/LaunchAgents"
    domain = f"gui/{os.getuid()}"
    plists = {role: launch_agent(role, settings, state_dir) for role in ROLES}
    if dry_run:
        return {"action": action, "plists": plists, "settings": str(settings), "side_effects": "none"}
    if sys.platform != "darwin":
        raise RuntimeError("launchd management requires macOS; use --dry-run on this platform")
    with InstanceLock(state_dir / "management.lock"):
        if action == "start":
            # Refuse to compete with the old global hotkey listener. Never terminate it.
            legacy = runner(["/bin/launchctl", "print", f"{domain}/com.lanshot.sender"], capture_output=True, text=True, check=False)
            old_script = runner(["/usr/bin/pgrep", "-f", "[s]ender_service.py run"], capture_output=True, text=True, check=False)
            if legacy.returncode == 0 or old_script.returncode == 0:
                raise Conflict("legacy sender is running; stop it with its own controller first")
            # Protect pre-existing P0/P1/foreign processes sharing this port.
            with socket.socket() as sock:
                sock.settimeout(1)
                occupied = sock.connect_ex(("127.0.0.1", int(data["port"]))) == 0
            if occupied:
                health(data)  # raises rather than killing a foreign process
            for role in ROLES:
                path = launch_dir / f"{LABELS[role]}.plist"
                if path.exists() and plistlib.loads(path.read_bytes()).get("ProgramArguments") != plists[role]["ProgramArguments"]:
                    raise Conflict("existing LaunchAgent is owned by another installation")
            data["enabled"] = True
            atomic_json(settings, data)
            private_dir(launch_dir)
            for role in ROLES:
                if reset_budget:
                    (state_dir / f"{role}-restart.json").unlink(missing_ok=True)
                path = launch_dir / f"{LABELS[role]}.plist"
                atomic_write(path, plistlib.dumps(plists[role]))
                active = runner(["/bin/launchctl", "print", f"{domain}/{LABELS[role]}"], capture_output=True, text=True, check=False)
                if active.returncode != 0:
                    runner(["/bin/launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True, check=True)
                else:
                    runner(["/bin/launchctl", "kickstart", f"{domain}/{LABELS[role]}"], capture_output=True, text=True, check=True)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    result = health(data)
                    sender_path = Config.load(Path(data["sender_config"])).spool_dir / "sender_status.json"
                    sender = json.loads(sender_path.read_text())
                    if result.get("storage") == "ready" and sender.get("status") in ("running", "degraded") and time.time() - sender["at"] < 5:
                        status = "ready" if sender["status"] == "running" and result.get("analyzer") == "configured_not_verified" else "degraded"
                        return {"status": status, "model": result["analyzer"], "input_error": sender.get("input_error"), "real_capture": "not_tested"}
                except (OSError, ValueError, KeyError, Conflict):
                    pass
                time.sleep(0.25)
            return {"status": "degraded", "reason": "readiness_not_confirmed", "logs": str(state_dir)}
        if action not in ("stop", "uninstall"):
            raise ValueError("unsupported action")
        data["enabled"] = False
        atomic_json(settings, data)  # explicit stop survives login/reboot
        still_loaded = []
        for role in reversed(ROLES):
            path = launch_dir / f"{LABELS[role]}.plist"
            if path.exists() and plistlib.loads(path.read_bytes()).get("ProgramArguments") != plists[role]["ProgramArguments"]:
                raise Conflict("refusing to stop another installation's agent")
            runner(["/bin/launchctl", "bootout", f"{domain}/{LABELS[role]}"], capture_output=True, text=True, check=False)
            check = runner(["/bin/launchctl", "print", f"{domain}/{LABELS[role]}"], capture_output=True, text=True, check=False)
            if check.returncode == 0:
                still_loaded.append(role)
            elif action == "uninstall":
                path.unlink(missing_ok=True)
        return {"status": "degraded" if still_loaded else "stopped_by_user",
                "still_loaded": still_loaded, "state_retained": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("configure")
    config.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    config.add_argument("--state-dir", type=Path, default=DEFAULT_DIR)
    config.add_argument("--port", type=int, default=8788)
    config.add_argument("--profile", default="default")
    config.add_argument("--prompt", type=Path, default=ROOT / "interview_prompt.txt")
    for name in ("start", "stop", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--reset-budget", action="store_true")
    child = sub.add_parser("run-child")
    child.add_argument("--role", choices=ROLES, required=True)
    child.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            configure(args.settings, state_dir=args.state_dir, port=args.port, profile=args.profile, prompt=args.prompt)
            print("P1 configured, disabled. No service started. Next inspect start --dry-run.")
            return 0
        if args.command == "run-child":
            return run_child(args.role, args.settings)
        # CLI and managed children read the same optional local authentication token.
        if sys.platform == "darwin" and not args.dry_run:
            token = keychain("LANSHOT_LOCAL_TOKEN", service="com.lanshot.p1")
            if token:
                os.environ["LANSHOT_LOCAL_TOKEN"] = token
        result = control(args.command, args.settings, dry_run=args.dry_run, reset_budget=args.reset_budget)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "degraded" else 0
    except Exception as error:
        print(json.dumps({"status": "failed", **error_info(error)}, ensure_ascii=False), file=sys.stderr)
        # CLI error strings are ours except IO; no provider bodies/credentials are emitted.
        if isinstance(error, (ConfigError, Conflict)):
            print(str(error), file=sys.stderr)
        return 0 if args.command == "run-child" and isinstance(error, (ValueError, KeyError, ConfigError, Conflict, OSError)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
