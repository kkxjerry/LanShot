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

from reliability import BUILD, Conflict, InstanceLock, TaskStore, atomic_json, atomic_write, error_info, private_dir, trace
from sender_service import Config, ConfigError

DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "LanShotP1"
DEFAULT_SETTINGS = DEFAULT_DIR / "settings.json"
ROLES = ("receiver", "receiver_backup", "sender", "display")
LABELS = {role: f"com.lanshot.p1.{role}" for role in ROLES}
ROOT = Path(__file__).resolve().parent


def read_prompt(path: Path) -> str:
    prompt = path.read_text().strip()
    if not prompt:
        raise ConfigError("prompt file is empty")
    return prompt


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
    if data.get("mode") in ("monolith", "redundant"):
        from multi_receiver import load_routes
        for field in ("routes_file", "display_dir", "overlay_app"):
            if not isinstance(data.get(field), str) or not Path(data[field]).is_absolute():
                raise ConfigError(f"{field} must be an absolute configured path")
        if config.routes_file != Path(data["routes_file"]) or config.display_dir != Path(data["display_dir"]):
            raise ConfigError("sender and display/routing settings differ")
        if not 1024 <= int(data.get("backup_port",0)) <= 65535 or int(data["backup_port"]) == int(data["port"]):
            raise ConfigError("backup port must be a distinct valid port")
        routes = load_routes(config.routes_file,config.profile)
        if (Path(routes["receiver_dir"]) != Path(data["receiver_dir"])
                or Path(routes["prompt_file"]) != Path(data["prompt_file"])):
            raise ConfigError("receiver store or prompt differs between routing and manager settings")
    elif data.get("mode") is not None:
        raise ConfigError("invalid deployment mode")
    return data


def configure(path: Path, *, state_dir: Path, port: int, profile: str, prompt: Path,
              mode: str = "redundant", backup_port: int | None = None, overlay_app: Path | None = None) -> None:
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
    if mode not in ("monolith", "redundant"):
        raise ConfigError("mode must be monolith or redundant")
    backup_port = backup_port if backup_port is not None else port + 1
    if not 1024 <= backup_port <= 65535 or backup_port == port:
        raise ConfigError("backup port must be a distinct valid port")
    receiver_dir = state_dir / "receiver"
    store = TaskStore(receiver_dir / "receiver_tasks.sqlite3")
    store.bind_receiver(profile,read_prompt(prompt))
    display_dir = state_dir / "display"
    routes_file = state_dir / "routes.json"
    endpoints = [{"name":"embedded","kind":"embedded","expected_cluster":store.origin}]
    if mode == "redundant":
        endpoints += [{"name":"local-primary","kind":"local","url":f"http://127.0.0.1:{port}","expected_cluster":store.origin},
                      {"name":"local-backup","kind":"local","url":f"http://127.0.0.1:{backup_port}","expected_cluster":store.origin}]
    atomic_json(routes_file,{"version":1,"profile":profile,"allow_remote":False,
        "receiver_dir":str(receiver_dir),"prompt_file":str(prompt),"endpoints":endpoints})
    Config(f"http://127.0.0.1:{port}", spool_dir=state_dir / "spool", profile=profile,
           routes_file=routes_file, display_dir=display_dir,
           blocked_words_file=Path.home() / ".config/lanshot-sender/blocked_words.txt").write(sender_config)
    app = (overlay_app or ROOT.parent/"capture-exclusion-demo/build/CaptureExclusionDemo.app").expanduser().resolve()
    atomic_json(path, {"state_dir":str(state_dir),"sender_config":str(sender_config),
        "receiver_dir":str(receiver_dir),"display_dir":str(display_dir),"routes_file":str(routes_file),
        "port":port,"backup_port":backup_port,"profile":profile,"prompt_file":str(prompt),
        "overlay_app":str(app),"mode":mode,"enabled":False})


def active_roles(data: dict) -> tuple[str, ...]:
    if data.get("mode") == "monolith":
        return ("sender", "display")
    if data.get("mode") == "redundant":
        return ROLES
    return ("receiver", "sender")  # Read-only compatibility with old settings.


def add_remote(settings: Path, *, url: str, token_env: str, allow_remote_images: bool,
               name: str = "remote", expected_cluster: str = "") -> None:
    from multi_receiver import Endpoint, load_routes
    if not allow_remote_images:
        raise ConfigError("explicit --allow-remote-images is required")
    data = read_settings(settings)
    if data["enabled"]:
        raise Conflict("stop managed services before changing routing policy")
    config = Config.load(Path(data["sender_config"]))
    if not config.routes_file:
        raise ConfigError("configure the revised deployment first")
    spec = load_routes(config.routes_file,config.profile)
    endpoint = Endpoint(name,"remote",url,token_env,expected_cluster)
    if any(e.name == name for e in spec["parsed_endpoints"]):
        raise Conflict("endpoint name already exists")
    del spec["parsed_endpoints"]
    spec["endpoints"].append(vars(endpoint))
    if len(spec["endpoints"]) > 8:
        raise ConfigError("at most eight routes are supported")
    spec["allow_remote"] = True
    atomic_json(config.routes_file,spec)


def validate_overlay(data: dict) -> Path:
    app = Path(data.get("overlay_app", ""))
    info = app / "Contents/Info.plist"
    if not info.is_file() or plistlib.loads(info.read_bytes()).get("LanShotDisplayProtocol") != 1:
        raise ConfigError("build the revised native app before starting display; old binaries do not support the new state directory")
    if not (app/"Contents/MacOS/CaptureExclusionDemo").is_file():
        raise ConfigError("native display executable is missing")
    return app


def open_native_display(data: dict, *, runner=subprocess.run) -> dict:
    app = validate_overlay(data)
    display = Path(data["display_dir"])
    try:
        status = json.loads((display/"overlay_status.json").read_text())
        if status.get("status") == "running" and status.get("protocol") == 1 and -2 <= time.time() - status["at"] < 5:
            return {"status":"already_open","display_dir":str(display)}
    except (OSError,ValueError,KeyError,TypeError):
        pass
    existing = runner(["/usr/bin/pgrep","-f","[C]aptureExclusionDemo.app/Contents/MacOS/CaptureExclusionDemo"],capture_output=True,text=True,check=False)
    if existing.returncode == 0:
        raise Conflict("an unverified native overlay is already running; close it explicitly instead of starting a second one")
    runner(["/usr/bin/open","-a",str(app),"--args","--lanshot-display-dir",str(display)],check=True)
    return {"status":"launch_requested_not_yet_rendered","display_dir":str(display)}


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
    if data.get("mode") not in ("monolith", "redundant"):
        raise ConfigError("old P1 settings are stop/read-only; configure the revised deployment first")
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
            config = Config.load(Path(data["sender_config"]))
            if config.routes_file:
                from multi_receiver import load_routes
                for endpoint in load_routes(config.routes_file,config.profile)["parsed_endpoints"]:
                    if endpoint.kind == "remote":
                        value = keychain(endpoint.token_env, service="com.lanshot.p1")
                        if value:
                            os.environ[endpoint.token_env] = value
                        else:
                            os.environ.pop(endpoint.token_env,None)
            if role in ("receiver", "receiver_backup"):
                import receiver_service
                receiver_dir = Path(data["receiver_dir"])
                os.environ["LANSHOT_PROMPT"] = read_prompt(Path(data["prompt_file"]))
                port = data["backup_port"] if role == "receiver_backup" else data["port"]
                code = receiver_service.main(["--host", "127.0.0.1", "--port", str(port), "--node", role,
                    "--profile", data["profile"], "--image", str(receiver_dir / "latest.jpg"),
                    "--answer-file", str(receiver_dir / "latest.txt"), "--history-dir", str(receiver_dir / "history")])
            elif role == "display":
                from display_bridge import run
                code = run(settings)
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


def current_readiness(data: dict[str, Any]) -> dict[str, Any] | None:
    config = Config.load(Path(data["sender_config"]))
    try:
        sender = json.loads((config.spool_dir / "sender_status.json").read_text())
        display = json.loads((Path(data["display_dir"]) / "display_status.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    now = time.time()
    if not (
        sender.get("build") == BUILD
        and display.get("build") == BUILD
        and sender.get("status") in ("running", "degraded")
        and display.get("status") == "running"
        and -2 <= now - sender["at"] < 5
        and -2 <= now - display["at"] < 5
    ):
        return None
    if data.get("mode") == "redundant":
        for role in ("receiver", "receiver_backup"):
            try:
                node = json.loads((Path(data["receiver_dir"]) / f"{role}_status.json").read_text())
            except (OSError, ValueError, TypeError):
                return None
            if not (
                node.get("build") == BUILD
                and node.get("status") == "running"
                and -2 <= now - node["at"] < 5
            ):
                return None
    ready = sender["status"] == "running"
    return {"status": "ready" if ready else "degraded", "input_error": sender.get("input_error"),
            "receiver": sender.get("receiver"), "display_bridge": display["status"],
            "model": "configured_not_verified", "real_capture": "not_tested", "mode": data.get("mode")}


def control(action: str, settings: Path, *, dry_run: bool = False, reset_budget: bool = False,
            runner: Callable[..., Any] = subprocess.run,
            launch_dir: Path | None = None) -> dict[str, Any]:
    data = read_settings(settings)
    state_dir = Path(data["state_dir"])
    launch_dir = launch_dir or Path.home() / "Library/LaunchAgents"
    domain = f"gui/{os.getuid()}"
    if action == "start" and data.get("mode") not in ("monolith", "redundant"):
        raise ConfigError("old P1 settings cannot start the revised runtime; configure it explicitly")
    roles = active_roles(data)
    plists = {role: launch_agent(role, settings, state_dir) for role in roles}
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
            if data.get("mode") != "monolith":
                for port in (data["port"], data.get("backup_port",data["port"])):
                    with socket.socket() as sock:
                        sock.settimeout(1)
                        occupied = sock.connect_ex(("127.0.0.1",int(port))) == 0
                    if occupied:
                        from multi_receiver import HTTPBackend,Endpoint
                        result = HTTPBackend(Endpoint("probe","local",f"http://127.0.0.1:{port}")).health()
                        cluster = TaskStore(Path(data["receiver_dir"])/"receiver_tasks.sqlite3").origin
                        if result.get("build") != BUILD or result.get("cluster_id") != cluster:
                            raise Conflict("port belongs to a different build/state directory")
            for role in roles:
                path = launch_dir / f"{LABELS[role]}.plist"
                if path.exists() and plistlib.loads(path.read_bytes()).get("ProgramArguments") != plists[role]["ProgramArguments"]:
                    raise Conflict("existing LaunchAgent is owned by another installation")
            data["enabled"] = True
            atomic_json(settings, data)
            private_dir(launch_dir)
            for role in roles:
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
                    status = current_readiness(data)
                    if status is not None:
                        return status
                except (OSError, ValueError, KeyError, Conflict):
                    pass
                time.sleep(0.25)
            return {"status": "degraded", "reason": "readiness_not_confirmed", "logs": str(state_dir)}
        if action not in ("stop", "uninstall"):
            raise ValueError("unsupported action")
        data["enabled"] = False
        atomic_json(settings, data)  # explicit stop survives login/reboot
        still_loaded = []
        for role in reversed(roles):
            path = launch_dir / f"{LABELS[role]}.plist"
            if path.exists() and plistlib.loads(path.read_bytes()).get("ProgramArguments") != plists[role]["ProgramArguments"]:
                raise Conflict("refusing to stop another installation's agent")
            runner(["/bin/launchctl", "bootout", f"{domain}/{LABELS[role]}"], capture_output=True, text=True, check=False)
            check = runner(["/bin/launchctl", "print", f"{domain}/{LABELS[role]}"], capture_output=True, text=True, check=False)
            if check.returncode == 0:
                still_loaded.append(role)
            elif action == "uninstall":
                path.unlink(missing_ok=True)
        if data.get("display_dir"):
            atomic_write(Path(data["display_dir"])/"page.txt",f"quit {time.time_ns()}\n".encode())
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
    config.add_argument("--mode", choices=("monolith","redundant"), default="redundant")
    config.add_argument("--backup-port", type=int)
    config.add_argument("--overlay-app", type=Path)
    config.add_argument("--prompt", type=Path, default=ROOT / "interview_prompt.txt")
    for name in ("start", "stop", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--reset-budget", action="store_true")
        command.add_argument("--expected-profile")
        command.add_argument("--open-display", action="store_true")
    remote = sub.add_parser("add-remote")
    remote.add_argument("--settings",type=Path,default=DEFAULT_SETTINGS)
    remote.add_argument("--url",required=True)
    remote.add_argument("--name",default="remote")
    remote.add_argument("--token-env",default="LANSHOT_REMOTE_TOKEN")
    remote.add_argument("--expected-cluster",default="")
    remote.add_argument("--allow-remote-images",action="store_true")
    child = sub.add_parser("run-child")
    child.add_argument("--role", choices=ROLES, required=True)
    child.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            configure(args.settings, state_dir=args.state_dir, port=args.port, profile=args.profile, prompt=args.prompt, mode=args.mode, backup_port=args.backup_port, overlay_app=args.overlay_app)
            print("P1 configured, disabled. No service started. Next inspect start --dry-run.")
            return 0
        if args.command == "add-remote":
            add_remote(args.settings,url=args.url,token_env=args.token_env,allow_remote_images=args.allow_remote_images,
                       name=args.name,expected_cluster=args.expected_cluster)
            print("Remote route configured; no image transmitted and no remote server deployed.")
            return 0
        if args.command == "run-child":
            return run_child(args.role, args.settings)
        # CLI and managed children read the same optional local authentication token.
        if sys.platform == "darwin" and not args.dry_run:
            token = keychain("LANSHOT_LOCAL_TOKEN", service="com.lanshot.p1")
            if token:
                os.environ["LANSHOT_LOCAL_TOKEN"] = token
        data = read_settings(args.settings)
        if args.expected_profile and args.expected_profile != data["profile"]:
            raise ConfigError("startup profile does not match settings; select the corresponding settings file")
        if args.open_display and args.command == "start":
            validate_overlay(data)
        result = control(args.command, args.settings, dry_run=args.dry_run, reset_budget=args.reset_budget)
        if args.open_display and args.command == "start" and not args.dry_run:
            result["native_display"] = open_native_display(data)
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
