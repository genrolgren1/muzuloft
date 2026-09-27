from __future__ import annotations

import argparse
import json
import os
import signal
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from capture_ldplayer_screenshot import capture_png_for_slot
from companion_runtime import CompanionRuntime
from companion_service import CompanionService
from runtime_guard import StateWatchdog
import atexit
from collections import deque

from ldplayer_backend import ensure_headless, probe_rok_renderers, serial_for_index, restart_instance, hide_instance_window, ld_foreground_package, ensure_normal_rok_foreground, FRIDA_TEST_PACKAGE

VERSION = "3.5.1"
ROOT = Path(__file__).resolve().parent
KS_HOME = Path(os.environ.get("KS_HOME") or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KingdomServices"))
DATA_DIR = KS_HOME / "gem_bot"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "gem_bot.log"
PID_PATH = DATA_DIR / "gem_bot.pid"
ACTIVE_LD_INDEX = max(0, int(os.environ.get("KS_LD_INDEX", "0") or 0))

DEFAULTS = {
    "slot": ACTIVE_LD_INDEX,
    "marches_to_send": 7,
    "auto_fill_all_marches": True,
    "gather_radius": 100,
    "gem_search_timeout_seconds": 240,
    "search_mode": "smart",
    "detector_mode": "strict",
    "temporal_confirmation": True,
    "ai_verify_borderline": True,

    "turbo_dispatch": True,
    "reliable_press_hold": True,
    "press_hold_ms": 180,
    "castle_hold_ms": 900,
    "tree_hold_ms": 750,
    "builtin_gem_finder": True,
    "gem_selector_strategy": "highest_first",
    "occupied_cooldown_seconds": 180,
    "ai_fallback": True,
    "debug_capture_failures": False,
    "loadout_color": "Blue",
    "daily_runtime_limit": 24,
    "retry_seconds": 90,
    "bring_marches_home": False,
}
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("GEM_BOT_SECRET") or os.urandom(32)

BOT_LOCK = threading.RLock()
BOT_PROCESS: subprocess.Popen | None = None
BOT_LOG_HANDLE = None
RESTART_STATE: dict[str, Any] = {"state": "idle", "error": "", "serial": ""}

ACCOUNT_LOCK = threading.RLock()
ACCOUNT_STATE: dict[str, Any] = {
    "state": "idle",
    "action": "",
    "message": "",
    "method": "",
    "screen": "",
    "error": "",
    "updated": 0,
}
ACCOUNT_TOKEN = secrets.token_urlsafe(32)
GATE_TOKEN = secrets.token_urlsafe(32)
GATE_URL = ""
COMPANION = CompanionRuntime(ROOT)

SERVICE_EVENTS = deque(maxlen=60)
LAST_STATUS = {"signature": None, "at": time.monotonic()}


def service_event(message):
    SERVICE_EVENTS.append({"time": time.strftime('%H:%M:%S'), "message": message})


def hidden_flags(detached: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            pass

    if "KS_LD_INDEX" in os.environ:
        cfg["slot"] = ACTIVE_LD_INDEX
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "slot": max(0, min(20, int(cfg.get("slot", 0) or 0))),
        "marches_to_send": max(1, min(7, int(cfg.get("marches_to_send", 7) or 7))),
        "auto_fill_all_marches": bool(cfg.get("auto_fill_all_marches", True)),
        "gather_radius": max(1, min(1000, int(cfg.get("gather_radius", 100) or 100))),
        "gem_search_timeout_seconds": 240,
        "search_mode": str(cfg.get("search_mode") or "smart") if str(cfg.get("search_mode") or "smart") in {"smart","fast","wide","eco"} else "smart",
        "detector_mode": str(cfg.get("detector_mode") or "strict") if str(cfg.get("detector_mode") or "strict") in {"strict","balanced","fast"} else "strict",
        "temporal_confirmation": bool(cfg.get("temporal_confirmation", True)),
        "ai_verify_borderline": bool(cfg.get("ai_verify_borderline", True)),
        "turbo_dispatch": bool(cfg.get("turbo_dispatch", True)),
        "reliable_press_hold": bool(cfg.get("reliable_press_hold", True)),
        "press_hold_ms": max(80, min(600, int(cfg.get("press_hold_ms", 180) or 180))),
        "castle_hold_ms": max(500, min(1800, int(cfg.get("castle_hold_ms", 900) or 900))),
        "tree_hold_ms": max(450, min(1600, int(cfg.get("tree_hold_ms", 750) or 750))),
        "builtin_gem_finder": bool(cfg.get("builtin_gem_finder", True)),
        "gem_selector_strategy": str(cfg.get("gem_selector_strategy") or "highest_first") if str(cfg.get("gem_selector_strategy") or "highest_first") in {"highest_first","lowest_first","cycle"} else "highest_first",
        "occupied_cooldown_seconds": max(30, min(900, int(cfg.get("occupied_cooldown_seconds", 180) or 180))),
        "ai_fallback": bool(cfg.get("ai_fallback", True)),
        "debug_capture_failures": bool(cfg.get("debug_capture_failures", False)),
        "loadout_color": str(cfg.get("loadout_color") or "Blue")[:32],
        "daily_runtime_limit": max(1, min(24, int(cfg.get("daily_runtime_limit", 24) or 24))),
        "retry_seconds": max(20, min(900, int(cfg.get("retry_seconds", 90) or 90))),
        "bring_marches_home": bool(cfg.get("bring_marches_home")),
    }
    CONFIG_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            cp = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                creationflags=hidden_flags(),
            )
            return str(pid) in (cp.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def bot_pid() -> int:
    global BOT_PROCESS
    if BOT_PROCESS is not None and BOT_PROCESS.poll() is None:
        return int(BOT_PROCESS.pid)
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            return pid if pid_alive(pid) else 0
        except Exception:
            pass
    return 0


def read_recent_log() -> str:
    try:
        with LOG_PATH.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 131072))
            return stream.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def log_tail(lines: int = 90) -> str:
    if not LOG_PATH.exists():
        return "Gem Bot standing by."
    try:
        raw = read_recent_log().splitlines()
        return "\n".join(raw[-lines:])
    except Exception:
        return "Could not read Gem Bot log."


def parsed_status() -> dict[str, Any]:
    status = {
        "state": "running" if bot_pid() else "stopped",
        "task": "idle",
        "actions": 0,
        "marches_sent": 0,
        "goals_done": 0,
        "runtime_seconds": 0,
        "note": "",
    }
    if LOG_PATH.exists():
        try:
            for line in reversed(read_recent_log().splitlines()):
                if "@@KS_STATUS@@" in line:
                    obj = json.loads(line.split("@@KS_STATUS@@", 1)[1])
                    if isinstance(obj, dict):
                        status.update(obj)
                        break
        except Exception:
            pass
    if not bot_pid() and status.get("state") == "running":
        status["state"] = "stopped"
    status["pid"] = bot_pid()
    status["log_tail"] = log_tail()
    if not status["pid"] and status.get("state") not in {"failed", "blocked"}:
        status["state"] = "stopped"
    status["companion"] = {**COMPANION.snapshot(), **COMPANION_SERVICE.snapshot()}
    status["dashboard_version"] = VERSION
    status["service_events"] = list(SERVICE_EVENTS)
    return status


def stop_bot() -> bool:
    global BOT_PROCESS, BOT_LOG_HANDLE
    with BOT_LOCK:
        pid = bot_pid()
        if not pid:
            return False
        try:
            if BOT_PROCESS is not None and BOT_PROCESS.poll() is None:
                BOT_PROCESS.terminate()
                try:
                    BOT_PROCESS.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    BOT_PROCESS.kill()
            elif os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=hidden_flags(),
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        BOT_PROCESS = None
        if BOT_LOG_HANDLE:
            try:
                BOT_LOG_HANDLE.close()
            except Exception:
                pass
            BOT_LOG_HANDLE = None
        PID_PATH.unlink(missing_ok=True)
        return True


def start_bot() -> int:
    global BOT_PROCESS, BOT_LOG_HANDLE
    with BOT_LOCK:
        existing = bot_pid()
        if existing:
            return existing

        if ACCOUNT_STATE.get('state') == 'running':
            raise RuntimeError('Wait for the current account action to finish')
        cfg = load_config()
        slot = int(cfg["slot"])
        COMPANION.require(slot)
        serial = ensure_headless(slot, launch_game=True)

        runtime_cfg = dict(cfg)
        runtime_cfg["adb_device"] = f"LDPLAYER:{slot}"
        runtime_path = DATA_DIR / "runtime_config.json"
        runtime_path.write_text(json.dumps(runtime_cfg, indent=2), encoding="utf-8")

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOT_LOG_HANDLE = LOG_PATH.open("w", encoding="utf-8", buffering=1)

        env = os.environ.copy()
        COMPANION.require(slot)
        env.update(GEMOPS_GATE_URL=GATE_URL, GEMOPS_GATE_TOKEN=GATE_TOKEN)

        BOT_PROCESS = subprocess.Popen(
            [sys.executable, str(ROOT / "gem_bot.py"), "--config", str(runtime_path)],
            cwd=str(ROOT),
            stdout=BOT_LOG_HANDLE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=hidden_flags(detached=True),
        )
        PID_PATH.write_text(str(BOT_PROCESS.pid), encoding="utf-8")
        return int(BOT_PROCESS.pid)


def screenshot_for_slot(slot: int) -> tuple[bytes, str]:
    slot = int(slot)

    # The normal dashboard is never supposed to display Frida Test Lab.
    # If an old test app is still foreground on the main instance, repair the
    # state once and immediately return to Rise of Kingdoms.
    try:
        current = ld_foreground_package(slot)
        if current.lower() == FRIDA_TEST_PACKAGE.lower():
            ensure_normal_rok_foreground(
                slot,
                wait_seconds=12,
                only_if_test_app=True,
            )
    except Exception:
        pass

    png = capture_png_for_slot(slot, attempts=2)
    return png, f"LDPLAYER:{slot}"


def restart_worker(slot: int) -> None:
    RESTART_STATE.update(state="restarting", error="", serial=serial_for_index(slot))
    try:
        stop_bot()
        restart_instance(slot)
        serial = ensure_headless(slot, launch_game=True)
        RESTART_STATE.update(state="ready", error="", serial=serial)
    except Exception as exc:
        RESTART_STATE.update(state="error", error=str(exc), serial=serial_for_index(slot))



@app.before_request
def protect_controls():
    if request.path == '/api/runtime/map':
        if not (secrets.compare_digest(request.headers.get('X-GemOps-Gate', ''), GATE_TOKEN)
                or secrets.compare_digest(request.headers.get('X-Account-Token', ''), ACCOUNT_TOKEN)):
            return jsonify(error='Unauthorized runtime'), 403
        return None
    if request.method == 'POST' and not secrets.compare_digest(request.headers.get('X-Account-Token', ''), ACCOUNT_TOKEN):
        return jsonify(error='Refresh Nexus before using its controls'), 403


@app.get("/")
def home():
    response = app.make_response(
        render_template(
            "index.html",
            cfg=load_config(),
            version=VERSION,
            account_token=ACCOUNT_TOKEN,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, version=VERSION, service="rok-gem-bot")


@app.get("/api/status")
def api_status():
    return jsonify(parsed_status())


@app.post("/api/config")
def api_config():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='Configuration must be a JSON object'), 400
    try:
        cfg = save_config(data)
    except (ValueError, TypeError) as exc:
        return jsonify(error='Invalid configuration value: ' + str(exc)), 400
    return jsonify(ok=True, config=cfg)


@app.post("/api/bot/start")
def api_bot_start():
    try:
        pid = start_bot()
        return jsonify(ok=True, pid=pid)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 409


@app.post("/api/bot/stop")
def api_bot_stop():
    return jsonify(ok=True, stopped=stop_bot())


@app.get("/api/screen")
def api_screen():
    cfg = load_config()
    try:
        png, serial = screenshot_for_slot(int(cfg["slot"]))
        return Response(
            png,
            mimetype="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "X-GemBot-Device": serial,
            },
        )
    except Exception as exc:
        return Response(str(exc), status=503, mimetype="text/plain")


@app.post("/api/emulator/start")
def api_emulator_start():
    cfg = load_config()
    try:
        serial = ensure_headless(int(cfg["slot"]), launch_game=True)
        return jsonify(ok=True, serial=serial)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.post("/api/emulator/restart")
def api_emulator_restart():
    cfg = load_config()
    if RESTART_STATE.get("state") == "restarting":
        return jsonify(ok=True, already_running=True)
    th = threading.Thread(target=restart_worker, args=(int(cfg["slot"]),), daemon=True)
    th.start()
    return jsonify(ok=True, state="restarting")


@app.get("/api/emulator/restart/status")
def api_restart_status():
    return jsonify(RESTART_STATE)


@app.post("/api/emulator/gpu-repair")
def api_gpu_repair():
    cfg = load_config()
    slot = int(cfg["slot"])
    try:
        stop_bot()
        serial = ensure_headless(slot, launch_game=False)
        serial = probe_rok_renderers(serial)
        return jsonify(ok=True, serial=serial)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.get("/api/frida/status")
def api_frida_status():
    return jsonify({**COMPANION.snapshot(), **COMPANION_SERVICE.snapshot()})


@app.get("/api/runtime/gate")
def api_runtime_gate():
    if not secrets.compare_digest(request.headers.get("X-GemOps-Gate", ""), GATE_TOKEN):
        return jsonify(error="Unauthorized runtime"), 403
    data = COMPANION.snapshot()
    return jsonify(passed=data['passed'], state=data['state'], error=data.get('error', ''))


@app.post('/api/runtime/map')
def api_runtime_map():
    if request.content_length is None or request.content_length > 3_000_000:
        return jsonify(error='Screenshot too large or missing size'), 413
    try:
        COMPANION.require(int(load_config()['slot']))
        return jsonify(COMPANION.analyze_map(request.get_data(cache=False)))
    except Exception as exc:
        return jsonify(error=str(exc)), 503


@app.post("/api/frida/<action>")
def api_frida_action(action):
    if action not in {"start", "restart", "stop", "prepare", "demo"}:
        return jsonify(error="Unknown companion action"), 404
    try:
        if action == "stop":
            COMPANION_SERVICE.stop()
        else:
            COMPANION_SERVICE.start(restart=action == 'restart')
        service_event('Companion ' + action + ' requested')
        return jsonify(ok=True, companion=COMPANION.snapshot())
    except Exception as exc:
        return jsonify(error=str(exc)), 409


def _account_progress_from_line(line: str) -> dict[str, Any] | None:
    prefix = "@@ACCOUNT_PROGRESS@@"
    if not str(line or "").startswith(prefix):
        return None
    try:
        obj = json.loads(str(line)[len(prefix):])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _account_result_from_output(text: str) -> dict[str, Any]:
    prefix = "@@ACCOUNT_RESULT@@"
    for line in reversed((text or "").splitlines()):
        if line.startswith(prefix):
            try:
                obj = json.loads(line[len(prefix):])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                break
    return {
        "ok": False,
        "blocked": True,
        "message": "Account worker returned no result",
    }


def _account_worker(req: dict[str, Any]) -> None:
    action = str(req.get("action") or "")
    ACCOUNT_STATE.update(
        state="running",
        action=action,
        message="Account action running",
        error="",
        updated=time.time(),
    )

    try:
        # Prevent the autonomous gem loop from tapping while an explicit account
        # operation is in progress.
        stop_bot()

        cfg = load_config()
        slot = int(cfg["slot"])
        ensure_headless(slot, launch_game=True)

        # Secret-bearing request goes over stdin. It is never put in argv or
        # written to the bot config/log.
        payload = json.dumps(req, ensure_ascii=False)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "account_controller.py"),
                "--device",
                f"LDPLAYER:{slot}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(ROOT),
            creationflags=hidden_flags(),
            bufsize=1,
        )

        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("Could not start account-control worker")

        proc.stdin.write(payload)
        proc.stdin.close()
        payload = ""

        result_lines: list[str] = []
        deadline = time.monotonic() + 420

        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(
                    cmd="account_controller.py",
                    timeout=420,
                )

            line = proc.stdout.readline()
            if line:
                line = line.rstrip("\r\n")
                progress = _account_progress_from_line(line)
                if progress is not None:
                    ACCOUNT_STATE.update(
                        state="running",
                        action=action,
                        message=str(progress.get("message") or "Working")[:500],
                        method=str(progress.get("method") or "")[:64],
                        screen=str(progress.get("screen") or "")[:120],
                        error="",
                        updated=time.time(),
                    )
                else:
                    # Keep only protocol/result lines in memory. Do not copy
                    # arbitrary worker output into the dashboard/log.
                    if line.startswith("@@ACCOUNT_RESULT@@"):
                        result_lines.append(line)
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.05)

        proc.wait(timeout=10)
        result = _account_result_from_output("\n".join(result_lines))
        if result.get("ok"):
            ACCOUNT_STATE.update(
                state="done",
                action=action,
                message=str(result.get("message") or "Account action completed")[:500],
                method=ACCOUNT_STATE.get("method", ""),
                screen=ACCOUNT_STATE.get("screen", ""),
                error="",
                updated=time.time(),
            )
        else:
            ACCOUNT_STATE.update(
                state="blocked" if result.get("blocked") else "error",
                action=action,
                message=str(result.get("message") or "Account action did not complete")[:500],
                method=ACCOUNT_STATE.get("method", ""),
                screen=ACCOUNT_STATE.get("screen", ""),
                error="",
                updated=time.time(),
            )
    except subprocess.TimeoutExpired:
        ACCOUNT_STATE.update(
            state="error",
            action=action,
            message="",
            error="Account action timed out",
            updated=time.time(),
        )
    except Exception as exc:
        ACCOUNT_STATE.update(
            state="error",
            action=action,
            message="",
            error=str(exc)[:500],
            updated=time.time(),
        )
    finally:
        # Remove all secret-bearing request fields from this worker's live state.
        for key in (
            "password",
            "current_password",
            "new_password",
            "confirm_password",
            "verification_code",
        ):
            if key in req:
                req[key] = ""
        req.clear()


@app.post("/api/gemops/self-test")
def api_gemops_self_test():
    try:
        cp = subprocess.run(
            [sys.executable, str(ROOT / "BOT_SELF_TEST.py")],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
            creationflags=hidden_flags(),
        )
        return jsonify(
            ok=cp.returncode == 0,
            output=(cp.stdout or "")[-10000:],
            returncode=cp.returncode,
        ), (200 if cp.returncode == 0 else 500)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.get("/api/gemops/diagnostics")
def api_gemops_diagnostics():
    cfg = load_config()
    template_dir = ROOT / "gem_templates"
    templates = sorted(p.stem for p in template_dir.glob("*.png")) if template_dir.exists() else []
    return jsonify(
        ok=True,
        version=VERSION,
        mode=cfg.get("search_mode", "smart"),
        radius=cfg.get("gather_radius", 100),
        search_timeout_seconds=240,
        templates=templates,
        template_count=len(templates),
        ai_fallback=bool(cfg.get("ai_fallback", True)),
        detector_mode=cfg.get("detector_mode", "strict"),
        turbo_dispatch=bool(cfg.get("turbo_dispatch", True)),
        temporal_confirmation=bool(cfg.get("temporal_confirmation", True)),
        debug_capture_failures=bool(cfg.get("debug_capture_failures", False)),
        foreground_package=ld_foreground_package(int(load_config()["slot"])),
        status=parsed_status(),
    )


@app.get("/api/account/status")
def api_account_status():
    response = jsonify(ACCOUNT_STATE)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/account/action")
def api_account_action():
    if request.headers.get("X-Account-Token", "") != ACCOUNT_TOKEN:
        return jsonify(ok=False, error="Invalid account-control token"), 403

    with ACCOUNT_LOCK:
        if ACCOUNT_STATE.get("state") == "running":
            return jsonify(ok=False, error="Another account action is still running"), 409

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error="Invalid account request"), 400

        action = str(data.get("action") or "").strip()
        allowed = {
            "open_dropdown",
            "choose_saved",
            "login_selected",
            "login_another",
            "prepare_email_code_login",
            "login_email_code",
            "login_credentials",
            "change_password",
        }
        if action not in allowed:
            return jsonify(ok=False, error="Unsupported account action"), 400

        # Validate password confirmation before starting the worker. Never place
        # password contents into ACCOUNT_STATE or response messages.
        if action == "prepare_email_code_login":
            if not str(data.get("email") or "").strip():
                return jsonify(ok=False, error="Email address is required"), 400

        if action == "login_email_code":
            if not str(data.get("email") or "").strip():
                return jsonify(ok=False, error="Email address is required"), 400
            if not str(data.get("verification_code") or "").strip():
                return jsonify(ok=False, error="Verification code is required"), 400

        if action == "login_credentials":
            if not str(data.get("username") or "").strip() or not str(data.get("password") or ""):
                return jsonify(ok=False, error="Email/username and password are required"), 400

        if action == "change_password":
            current = str(data.get("current_password") or "")
            new = str(data.get("new_password") or "")
            confirm = str(data.get("confirm_password") or "")
            if not current or not new or not confirm:
                return jsonify(ok=False, error="Current, new, and confirm password are required"), 400
            if new != confirm:
                return jsonify(ok=False, error="New passwords do not match"), 400
            data.pop("confirm_password", None)

        stop_bot()  # Account navigation and mission input must never race.
        service_event('Mission stopped for account control')
        # Copy only the current request into a daemon worker. The web response
        # never echoes secret fields.
        worker_req = dict(data)
        threading.Thread(
            target=_account_worker,
            args=(worker_req,),
            daemon=True,
        ).start()

    return jsonify(ok=True, state="running")



COMPANION_SERVICE = CompanionService(COMPANION, lambda: int(load_config()['slot']),
                                     lambda: stop_bot(), lambda msg: service_event(msg))
atexit.register(COMPANION_SERVICE.stop)


def _service_watchdog() -> None:
    residence = StateWatchdog()
    while True:
        try:
            if bot_pid():
                state = parsed_status()
                signature = (state.get('runtime_seconds'), state.get('task'), state.get('actions'))
                if signature != LAST_STATUS['signature']:
                    LAST_STATUS.update(signature=signature, at=time.monotonic())
                residence.enter(state.get('task', 'starting'))
                idle = state.get('task') in {'waiting_for_marches', 'alternate_task_pending'}
                reason = ''
                if not COMPANION.snapshot()['passed']:
                    reason = 'Required companion health lost'
                elif (state.get('watchdog_overdue') or residence.snapshot()['watchdog_overdue']) and not idle:
                    reason = 'State deadline exceeded: ' + str(state.get('task'))
                elif time.monotonic() - LAST_STATUS['at'] > (960 if idle else 90):
                    reason = 'Bot status heartbeat stalled'
                if reason:
                    stop_bot()
                    service_event(reason + '; mission stopped. Inspect and restart when ready.')
            else:
                residence.enter('stopped')
                LAST_STATUS.update(signature=None, at=time.monotonic())
        except Exception as exc:
            service_event('Watchdog error: ' + str(exc))
        time.sleep(1)


def _headless_guard() -> None:
    headless = os.environ.get("KS_LD_HEADLESS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    if not headless:
        return

    while True:
        try:
            hide_instance_window(ACTIVE_LD_INDEX)
        except Exception:
            pass
        time.sleep(0.75)



def main() -> int:
    global GATE_URL
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("GEM_BOT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("GEM_BOT_PORT", "8080")))
    args = p.parse_args()
    GATE_URL = f"http://127.0.0.1:{args.port}/api/runtime/gate"
    threading.Thread(target=_service_watchdog, daemon=True).start()

    save_config(load_config())
    threading.Thread(target=COMPANION_SERVICE.run, daemon=True).start()

    threading.Thread(target=_headless_guard, daemon=True).start()

    from waitress import serve
    print(f"[GEM WEB] RoK Gem Bot v{VERSION} on http://{args.host}:{args.port}", flush=True)
    serve(app, host=args.host, port=args.port, threads=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
