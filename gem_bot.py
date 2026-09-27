from __future__ import annotations

import argparse
import base64
import io
import math
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from fast_gem_ui import FastGemUI
from search_brain import SearchBrain
from session_metrics import SessionMetrics
from random_map_search import SearchPoint, radius_units

from ldplayer_backend import ensure_for_scanner, find_adb as find_managed_adb, ensure_rok_running, rok_is_foreground, parse_ldplayer, ld_screenshot_bytes, ld_tap, ld_swipe, ld_ensure_rok_running, ld_rok_is_foreground

VERSION = "3.5.1"
MODEL = os.environ.get("KS_FARM_MODEL", "qwen3-vl:2b")
OLLAMA = "http://127.0.0.1:11434"
COMMON_ADB_PORTS = (5555, 7555, 16384, 21503, 62001)


def log(msg: str) -> None:
    print(msg, flush=True)


def emit_status(**data: Any) -> None:
    print("@@KS_STATUS@@" + json.dumps(data, ensure_ascii=False, separators=(",", ":")), flush=True)


def run(cmd: list[str], timeout: int = 15, check: bool = True) -> str:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Executable not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}") from exc
    text = p.stdout.decode(errors="ignore").strip()
    if check and p.returncode != 0:
        raise RuntimeError(text or f"Command failed ({p.returncode}): {' '.join(cmd)}")
    return text


def find_adb() -> str:
    candidates: list[str] = []
    env = os.environ.get("ADB_PATH", "").strip()
    if env:
        candidates.append(env)
    which = shutil.which("adb")
    if which:
        candidates.append(which)
    candidates += [
        r"D:\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        r"D:\Program Files\Netease\MuMuPlayer\nx_main\adb.exe",
        r"C:\Program Files\Netease\MuMuPlayer\nx_main\adb.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(Path(c))
    raise RuntimeError("ADB was not found. Put adb.exe on PATH or in D:\\platform-tools\\adb.exe")


def adb_devices(adb: str) -> list[tuple[str, str]]:
    text = run([adb, "devices", "-l"], check=False)
    out: list[tuple[str, str]] = []
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) >= 2:
            out.append((p[0], p[1]))
    return out


def adb_shell(adb: str, serial: str, *args: str, timeout: int = 10) -> str:
    return run([adb, "-s", serial, "shell", *args], timeout=timeout, check=False)


def android_id(adb: str, serial: str) -> str:
    for cmd in (("settings", "get", "secure", "android_id"), ("getprop", "ro.serialno")):
        v = adb_shell(adb, serial, *cmd).strip()
        if v and v.lower() not in {"null", "unknown", "none"}:
            return v
    return ""


def foreground(adb: str, serial: str) -> str:
    t = adb_shell(adb, serial, "dumpsys", "window", "windows", timeout=12)
    lines = [x.strip() for x in t.splitlines() if "mCurrentFocus" in x or "mFocusedApp" in x]
    if not lines:
        t = adb_shell(adb, serial, "dumpsys", "activity", "activities", timeout=12)
        lines = [x.strip() for x in t.splitlines() if "mResumedActivity" in x or "ResumedActivity" in x]
    return " | ".join(lines)[:1000]


def is_rok_focus(text: str) -> bool:
    low = text.lower()
    return any(x in low for x in ("com.lilithgame.roc", "lilithgame.roc", "roc.gp", "riseofkingdoms"))


def choose_device(adb: str, preferred: str = "") -> str:
    run([adb, "start-server"], check=False)
    preferred = (preferred or "AUTO").strip()
    online = [s for s, state in adb_devices(adb) if state == "device"]
    for port in COMMON_ADB_PORTS:
        ep = f"127.0.0.1:{port}"
        if ep not in online:
            run([adb, "connect", ep], timeout=4, check=False)
    online = [s for s, state in adb_devices(adb) if state == "device"]
    if not online:
        raise RuntimeError("No ADB emulator is online. Open the emulator and Rise of Kingdoms first.")
    if preferred.upper() != "AUTO":
        if preferred not in online:
            raise RuntimeError(f"Configured device {preferred!r} is not online. Online: {', '.join(online)}")
        return preferred
    if len(online) == 1:
        return online[0]
    groups: dict[str, list[str]] = {}
    for serial in online:
        groups.setdefault(android_id(adb, serial) or serial, []).append(serial)
    if len(groups) == 1:
        aliases = next(iter(groups.values()))
        def pref(s: str) -> tuple[int, str]:
            if s.endswith(":5555"): return (0, s)
            if s.startswith("emulator-"): return (1, s)
            if s.endswith(":7555"): return (2, s)
            return (5, s)
        chosen = sorted(aliases, key=pref)[0]
        log(f"[ADB] Alias ports collapsed: {', '.join(aliases)} -> {chosen}")
        return chosen
    rok: list[str] = []
    for serial in online:
        f = foreground(adb, serial)
        log(f"[ADB] {serial} foreground: {f[:140] or '(unknown)'}")
        if is_rok_focus(f):
            rok.append(serial)
    if len(rok) == 1:
        return rok[0]
    raise RuntimeError("AUTO found multiple different emulators and could not uniquely identify the RoK account.")


from runtime_guard import RuntimeGuard, StateWatchdog


class ADB:
    def __init__(self, preferred: str = "LDPLAYER:0"):
        self.guard = RuntimeGuard()
        self.guard.require()
        preferred = (preferred or "LDPLAYER:0").strip()
        resolved = ensure_for_scanner(preferred)
        self.adb = find_managed_adb()
        self.ld_index = parse_ldplayer(resolved)

        if self.ld_index is not None:
            self.device = f"LDPLAYER:{self.ld_index}"
            try:
                ld_ensure_rok_running(self.ld_index, wait_seconds=8.0)
            except Exception as exc:
                log(f"[ROK] Foreground check warning: {exc}")
            log(f"[ADB] Backend: LDPlayer index {self.ld_index}")
            log(f"[ADB] Target: {self.device}")
            return

        # Explicit non-LDPlayer serial fallback.
        if resolved and resolved.upper() != "AUTO":
            self.device = resolved
        else:
            self.device = choose_device(self.adb, "AUTO")

        try:
            ensure_rok_running(self.adb, self.device, wait_seconds=8.0)
        except Exception as exc:
            log(f"[ROK] Foreground check warning: {exc}")

        log(f"[ADB] Executable: {self.adb}")
        log(f"[ADB] Device: {self.device}")

    def screenshot(self) -> bytes:
        self.guard.require()
        if self.ld_index is not None:
            return self._checked_screen(ld_screenshot_bytes(self.ld_index))

        data = subprocess.check_output(
            [self.adb, "-s", self.device, "exec-out", "screencap", "-p"],
            timeout=20,
        )
        if not data.startswith(b"\x89PNG"):
            raise RuntimeError("ADB screenshot was not valid PNG")
        return self._checked_screen(data)

    def tap(self, x: int, y: int) -> None:
        self.guard.require()
        if self.ld_index is not None:
            ld_tap(self.ld_index, x, y)
            return
        run([self.adb, "-s", self.device, "shell", "input", "tap", str(int(x)), str(int(y))])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 500) -> None:
        self.guard.require()
        if self.ld_index is not None:
            ld_swipe(self.ld_index, x1, y1, x2, y2, ms)
            return
        run([
            self.adb, "-s", self.device, "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(ms),
        ])

    def back(self) -> None:
        # Android Back on the world map opens the game's exit confirmation.
        self.guard.require()
        raise RuntimeError("Automatic Android Back is disabled; inspect the current panel")

    def _checked_screen(self, data: bytes) -> bytes:
        ui = getattr(self, '_safety_ui', None)
        if ui is None:
            ui = self._safety_ui = FastGemUI()
        if ui.find(data, 'exit_game_notice', threshold=0.94, region=(0.24, 0.25, 0.76, 0.61)):
            raise RuntimeError("Exit-game dialog detected. Mission stopped. Choose CANCEL in RoK before restarting.")
        return data

    def rok_foreground(self) -> bool:
        if self.ld_index is not None:
            return ld_rok_is_foreground(self.ld_index)
        return rok_is_foreground(self.adb, self.device)

    def ensure_rok(self) -> bool:
        self.guard.require()
        if self.ld_index is not None:
            return ld_ensure_rok_running(self.ld_index, wait_seconds=8.0)
        return ensure_rok_running(self.adb, self.device, wait_seconds=8.0)



class Vision:
    def __init__(self):
        self.model = MODEL
        self.ensure()

    @staticmethod
    def _json(url: str, timeout: int = 10) -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def ensure(self) -> None:
        # Mission search budgets must never be consumed by a model download.
        tags = self._json(OLLAMA + "/api/tags", 2)
        names = {str(x.get("name") or "") for x in tags.get("models", [])}
        if self.model not in names:
            raise RuntimeError("Optional AI is not prepared. Run PREPARE_GEM_VISION.bat before a mission.")

    @staticmethod
    def _resize(png: bytes, width: int = 960) -> tuple[bytes, int, int]:
        with Image.open(io.BytesIO(png)) as im:
            src = im.convert("RGB")
            ow, oh = src.size
            if src.width > width:
                nh = max(1, round(src.height * width / src.width))
                src = src.resize((width, nh), Image.Resampling.LANCZOS)
            out = io.BytesIO(); src.save(out, "JPEG", quality=88)
            return out.getvalue(), ow, oh

    @staticmethod
    def _extract(text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        try:
            v = json.loads(raw)
            if isinstance(v, dict): return v
        except Exception:
            pass
        dec = json.JSONDecoder()
        for m in re.finditer(r"\{", raw):
            try:
                v, _ = dec.raw_decode(raw[m.start():])
                if isinstance(v, dict): return v
            except Exception:
                continue
        raise RuntimeError("AI returned no usable JSON")

    def next_action(self, png: bytes, goal: str, context: str = "") -> dict[str, Any]:
        image, ow, oh = self._resize(png)
        schema = {
            "type":"object","additionalProperties":False,
            "properties":{
                "status":{"type":"string","enum":["act","done","blocked","wait"]},
                "action":{"type":"string","enum":["tap","swipe","back","wait","none"]},
                "x":{"type":"number","minimum":0,"maximum":1},
                "y":{"type":"number","minimum":0,"maximum":1},
                "x2":{"type":"number","minimum":0,"maximum":1},
                "y2":{"type":"number","minimum":0,"maximum":1},
                "confidence":{"type":"number","minimum":0,"maximum":1},
                "screen":{"type":"string"},
                "reason":{"type":"string"},
            },
            "required":["status","action","x","y","x2","y2","confidence","screen","reason"],
        }
        prompt = f"""
You are the visual copilot for a Rise of Kingdoms GEM GATHERING automation running on a dedicated emulator.
Current goal: {goal}
Context: {context or 'none'}

Choose ONE safe next UI action based only on the screenshot.
Coordinates x/y and x2/y2 are normalized 0..1 across the full screenshot.
Return status=done ONLY when the goal is visibly completed.
Return blocked if the goal cannot proceed right now (for example no free march or no node found).

STRICT SAFETY / CORRECTNESS RULES:
- Never attack a player city, alliance structure, or player march.
- Never delete/disband troops, spend premium gems, buy paid bundles, change account/login/security settings, or send chat.
- LOGIN CONTINUATION EXCEPTION: when the current goal explicitly says to resume the already-selected saved account, you MAY press the visible Login button for that already-selected account only. Do NOT open the account dropdown, choose another account, enter/change credentials, use "Log in to another account", or change any account/security setting.
- Never confirm a purchase unless the goal explicitly names that exact free/resource purchase and its configured budget allows it.
- Gathering goal means neutral resource nodes only. For gem gathering, select a neutral GEM DEPOSIT only.
- If uncertain about a destructive or premium-spend action, return blocked.
- Prefer obvious labeled buttons and normal navigation. Do not guess hidden controls.
""".strip()
        payload = {
            "model": self.model,
            "messages":[{"role":"user","content":prompt,"images":[base64.b64encode(image).decode("ascii")]}],
            "stream":False,"format":schema,"think":False,"keep_alive":"30m",
            "options":{"temperature":0,"num_predict":500},
        }
        req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type":"application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=25) as r:
            obj = json.loads(r.read().decode("utf-8"))
        msg = obj.get("message") or {}
        for f in ("content", "thinking"):
            txt = str(msg.get(f) or "").strip()
            if txt:
                try:
                    ans = self._extract(txt)
                    ans["_ow"] = ow; ans["_oh"] = oh
                    log(f"[AI] {ans.get('screen','screen')} -> {ans.get('status')}/{ans.get('action')} ({time.time()-t0:.1f}s)")
                    return ans
                except Exception:
                    pass
        raise RuntimeError("AI returned no usable action")


    def classify_gem_candidate(self, png: bytes, timeout: float = 20) -> dict[str, Any]:
        """
        Read-only classifier for an already-cropped candidate.
        It never returns coordinates or UI actions.
        """
        image, _, _ = self._resize(png, width=520)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "class": {
                    "type": "string",
                    "enum": [
                        "free_gem",
                        "occupied_gem",
                        "not_gem",
                        "uncertain",
                    ],
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "reason": {"type": "string"},
            },
            "required": ["class", "confidence", "reason"],
        }

        prompt = """
Classify ONLY the object near the center of this cropped Rise of Kingdoms image.

free_gem:
- a neutral red crystal GEM DEPOSIT
- no gathering/occupation axe badge
- not visibly already occupied

occupied_gem:
- it is a gem node but has an active axe/gathering badge or is visibly occupied
- axe badge may be green, blue, red, or another color

not_gem:
- city, troop, commander, UI icon, building, other resource, decorative object,
  terrain, red object that is not a gem deposit, or anything else

uncertain:
- crop is too ambiguous to safely decide

Accuracy is more important than accepting a candidate.
Do not guess free_gem when uncertain.
""".strip()

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [
                        base64.b64encode(image).decode("ascii")
                    ],
                }
            ],
            "stream": False,
            "format": schema,
            "think": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0,
                "num_predict": 180,
            },
        }
        req = urllib.request.Request(
            OLLAMA + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=max(1.0, min(20.0, timeout))) as r:
            obj = json.loads(r.read().decode("utf-8"))

        msg = obj.get("message") or {}
        for field in ("content", "thinking"):
            text = str(msg.get(field) or "").strip()
            if not text:
                continue
            try:
                ans = self._extract(text)
                log(
                    f"[AI VERIFY] {ans.get('class')} "
                    f"conf={float(ans.get('confidence') or 0):.2f} "
                    f"({time.time()-t0:.1f}s)"
                )
                return ans
            except Exception:
                continue
        raise RuntimeError("AI candidate verifier returned no usable result")


DEFAULTS: dict[str, Any] = {
    "adb_device":"LDPLAYER:0",
    "marches_to_send":7,
    "auto_fill_all_marches":True,
    "gather_radius":100,
    "gem_search_timeout_seconds":240,
    "search_mode":"smart",
    "detector_mode":"strict",
    "temporal_confirmation":True,
    "ai_verify_borderline":True,

    "turbo_dispatch":True,
    "reliable_press_hold":True,
    "press_hold_ms":180,
    "castle_hold_ms":900,
    "tree_hold_ms":750,
    "builtin_gem_finder":True,
    "gem_selector_strategy":"highest_first",
    "occupied_cooldown_seconds":180,
    "ai_fallback":True,
    "debug_capture_failures":False,
    "loadout_color":"Blue",
    "daily_runtime_limit":24,
    "retry_seconds":90,
    "bring_marches_home":False,
}



class GemBot:
    def __init__(self, config: dict[str, Any]):
        self.cfg = DEFAULTS | (config or {})
        self.adb = ADB(str(self.cfg.get("adb_device") or "AUTO"))
        # Local computer vision is the primary engine. Ollama is loaded lazily
        # only when a screen cannot be resolved locally.
        self.ai = None
        self.fast_ui = FastGemUI()
        self.search_brain = SearchBrain(str(self.cfg.get("search_mode") or "smart"))
        self.metrics = SessionMetrics()
        self.watchdog = StateWatchdog()
        self._dispatch_pending = False
        self._gem_dispatch_owned = False
        self._rescue_after = 0.0
        self._finder_failures = 0
        self._finder_open = False
        self._finder_after = 0.0
        self._cycle_deadline = 0.0
        self.started = time.time()
        self.search_started_monotonic = 0.0
        self.search_timeout_seconds = 240.0
        self.debug_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "KingdomServices" / "gem_bot" / "debug"
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.search_swipe_index = 0
        self.search_point = SearchPoint()
        self.search_timed_out = False
        self.alternate_task_requested = False
        self.last_failure_code = ""
        self.last_march_used = None
        self.last_march_total = None
        self.occupied_gem_targets: list[tuple[float, float, float]] = []
        self._search_rng = random.Random()
        self.actions = 0
        self.marches_sent = 0
        self.goals_done = 0
        self.last_task = "starting"
        self.last_ai_reason = ""
        self.last_ai_screen = ""
        self.last_ai_status = ""
        self.candidate_events: list[dict[str, Any]] = []
        self.last_candidate: dict[str, Any] = {}
        self.preferred_gem_scale: float | None = None
        self.selector_attempt = 0
        self.last_selector_count = 0

    def _vision(self) -> Vision:
        if self.ai is None:
            if not bool(self.cfg.get("ai_fallback", True)):
                raise RuntimeError("AI fallback is disabled in GemOps settings")
            log(f"[AI] Local vision needs help -> loading {MODEL} on demand")
            self.ai = Vision()
            self.metrics.inc("ai_model_starts")
        return self.ai

    def _debug_capture(self, reason: str, png: bytes | None = None) -> None:
        if not bool(self.cfg.get("debug_capture_failures", False)):
            return
        try:
            if png is None:
                png = self.adb.screenshot()
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason)[:48] or "debug"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = self.debug_dir / f"{stamp}_{safe}.png"
            path.write_bytes(png)
            files = sorted(self.debug_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for old in files[:-30]:
                old.unlink(missing_ok=True)
            self.metrics.inc("debug_captures")
        except Exception as exc:
            log(f"[DEBUG] Capture warning: {exc}")

    def _record_candidate(
        self,
        verdict: str,
        hit: dict[str, Any],
        analysis: dict[str, Any],
        *,
        image_width: int,
        image_height: int,
        reason: str = "",
    ) -> None:
        event = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "verdict": str(verdict),
            "confidence": round(float(analysis.get("confidence") or 0.0), 3),
            "template": round(float(analysis.get("template") or 0.0), 3),
            "hist": round(float(analysis.get("hist") or 0.0), 3),
            "red": round(float(analysis.get("red_match") or 0.0), 3),
            "edge": round(float(analysis.get("edge") or 0.0), 3),
            "axe": round(float(analysis.get("axe") or 0.0), 3),
            "green": round(float(analysis.get("green") or 0.0), 4),
            "reason": str(reason or analysis.get("reason") or "")[:160],
            "x": round(float(hit["x"]) / max(1.0, image_width), 4),
            "y": round(float(hit["y"]) / max(1.0, image_height), 4),
            "w": round(float(hit["w"]) / max(1.0, image_width), 4),
            "h": round(float(hit["h"]) / max(1.0, image_height), 4),
            "scale": round(float(hit.get("scale") or 0.0), 3),
        }
        self.last_candidate = event
        self.candidate_events.append(event)
        self.candidate_events = self.candidate_events[-12:]

    def _preferred_gem_scales(self) -> tuple[float, ...] | None:
        """
        Fast first-pass scales around the most recently confirmed free gem.

        If map zoom changes and this pass misses, the caller immediately falls
        back to the full GEM_SCALES range.
        """
        if self.preferred_gem_scale is None:
            return None

        base = float(self.preferred_gem_scale)
        values = {
            max(0.26, min(1.50, base * factor))
            for factor in (0.82, 0.90, 0.96, 1.00, 1.04, 1.10, 1.20)
        }
        return tuple(sorted(round(x, 3) for x in values))

    def _scan_gem_candidates(self, png, *, threshold, max_results):
        start = time.monotonic()
        try:
            return self._scan_gem_candidates_impl(png, threshold=threshold, max_results=max_results)
        finally:
            self.metrics.timing("detector", time.monotonic() - start)

    def _scan_gem_candidates_impl(
        self,
        png: bytes,
        *,
        threshold: float,
        max_results: int,
    ) -> list[dict[str, Any]]:
        # Frida scans screenshot pixels only inside our companion. These are
        # proposals, never authorization to tap: local/temporal validation stays.
        try:
            t0 = time.monotonic()
            proposals = self.adb.guard.analyze_map(png)
            self.metrics.timing('frida_map', time.monotonic()-t0)
            if isinstance(proposals, dict):
                self.metrics.inc('frida_map_scans')
                regional = []
                for region in proposals.get('regions', [])[:12]:
                    if not isinstance(region, list) or len(region) != 4:
                        continue
                    if not (0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1):
                        continue
                    regional.extend(self.fast_ui.find_all(png, 'gem_node', threshold=threshold,
                        region=tuple(region), max_results=max_results))
                regional.sort(key=lambda hit: hit['score'], reverse=True)
                distinct = []
                for hit in regional:
                    if not any(self.fast_ui._iou(hit, prior) >= .34 for prior in distinct):
                        distinct.append(hit)
                # A rejected proposal must never suppress the full local scan.
                if any(self.fast_ui.classify_gem_candidate(png, hit,
                    mode=str(self.cfg.get('detector_mode') or 'strict')).get('verdict') in
                    {'accept', 'borderline'} for hit in distinct[:max_results]):
                    self.metrics.inc('frida_map_candidate_frames')
                    return distinct[:max_results]
        except Exception as exc:
            self.metrics.inc('frida_map_fallbacks')
        preferred = self._preferred_gem_scales()

        if preferred:
            hits = self.fast_ui.find_all(
                png,
                "gem_node",
                threshold=threshold,
                region=(0.035, 0.055, 0.965, 0.915),
                max_results=max_results,
                scales=preferred,
            )
            self.metrics.inc("preferred_scale_scans")
            if hits and any(self.fast_ui.classify_gem_candidate(
                png, hit, mode=str(self.cfg.get('detector_mode') or 'strict')
            ).get('verdict') in {'accept', 'borderline'} for hit in hits):
                self.metrics.inc("preferred_scale_hits")
                return hits

        # Full zoom-adaptive fallback.
        self.metrics.inc("full_scale_scans")
        return self.fast_ui.find_all(
            png,
            "gem_node",
            threshold=threshold,
            region=(0.035, 0.055, 0.965, 0.915),
            max_results=max_results,
        )

    def _temporal_confirm_candidate(
        self,
        first_hit: dict[str, Any],
        *,
        detector_mode: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """
        Candidate must remain a valid gem across a second frame before we tap it.
        This blocks short animation/UI/template artifacts.
        """
        if not bool(self.cfg.get("temporal_confirmation", True)):
            return True, None

        time.sleep(0.11)
        second_png = self.adb.screenshot()
        first_scale = float(first_hit.get("scale") or 0.0)
        temporal_scales = None
        if first_scale > 0:
            temporal_scales = tuple(
                sorted(
                    {
                        round(
                            max(
                                0.26,
                                min(1.50, first_scale * factor),
                            ),
                            3,
                        )
                        for factor in (
                            0.86, 0.93, 0.98, 1.00, 1.03, 1.08, 1.16
                        )
                    }
                )
            )

        second_hits = self.fast_ui.find_all(
            second_png,
            "gem_node",
            threshold=0.52,
            region=(0.035, 0.055, 0.965, 0.915),
            max_results=14,
            scales=temporal_scales,
        )

        max_distance = max(
            22.0,
            float(max(first_hit["w"], first_hit["h"])) * 0.58,
        )
        nearest = None
        nearest_distance = 10**9

        for hit in second_hits:
            distance = math.hypot(
                hit["cx"] - first_hit["cx"],
                hit["cy"] - first_hit["cy"],
            )
            if distance <= max_distance and distance < nearest_distance:
                nearest = hit
                nearest_distance = distance

        if nearest is None:
            self.metrics.inc("temporal_rejections")
            self.metrics.inc("false_positive_prevented")
            return False, None

        analysis = self.fast_ui.classify_gem_candidate(
            second_png,
            nearest,
            mode=detector_mode,
        )

        if analysis.get("verdict") in {"occupied", "reject"}:
            self.metrics.inc("temporal_rejections")
            self.metrics.inc("false_positive_prevented")
            return False, analysis

        # Borderline is allowed here only when the first frame was already
        # locally acceptable. AI can still verify it afterward if configured.
        # Tap the current position only after conservative displacement checks.
        if nearest_distance > max(8.0, min(first_hit['w'], first_hit['h']) * 0.25):
            self.metrics.inc("temporal_rejections")
            return False, analysis
        first_hit.update({k: nearest[k] for k in ('x','y','cx','cy','w','h')})
        return True, analysis

    def _ai_verify_candidate(
        self,
        png: bytes,
        hit: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        self.metrics.inc("ai_candidate_verifications")
        crop = self.fast_ui.crop_candidate(png, hit)
        try:
            remaining = self._cycle_deadline - time.monotonic() if self._cycle_deadline else 20
            if remaining < 5:
                return False, {"class":"uncertain", "confidence":0, "reason":"search deadline near"}
            result = self._vision().classify_gem_candidate(crop, timeout=min(20, remaining - 3))
        except Exception as exc:
            self.metrics.inc("ai_candidate_errors")
            return False, {
                "class": "uncertain",
                "confidence": 0.0,
                "reason": str(exc),
            }

        cls = str(result.get("class") or "uncertain")
        conf = float(result.get("confidence") or 0.0)
        if cls == "free_gem" and conf >= 0.72:
            self.metrics.inc("ai_candidate_accepts")
            return True, result

        self.metrics.inc("ai_candidate_rejections")
        self.metrics.inc("false_positive_prevented")
        return False, result

    def _metric_status(self) -> dict[str, Any]:
        radius = max(1, min(1000, int(self.cfg.get("gather_radius") or 100)))
        data = self.metrics.snapshot()
        data.update(self.watchdog.snapshot())
        data.update(self.search_brain.snapshot(radius))
        data.update({
            "failure_code": self.last_failure_code,
            "march_used": self.last_march_used,
            "march_total": self.last_march_total,
            "search_x": round(self.search_point.x, 2),
            "search_y": round(self.search_point.y, 2),
            "occupied_memory": len(self.occupied_gem_targets),
            "detector_mode": str(self.cfg.get("detector_mode") or "strict"),
            "candidate_events": list(self.candidate_events),
            "last_candidate": dict(self.last_candidate),
            "search_radius_units": round(radius_units(radius), 2),
            "turbo_dispatch": bool(self.cfg.get("turbo_dispatch", True)),
            "preferred_gem_scale": (
                round(float(self.preferred_gem_scale), 3)
                if self.preferred_gem_scale is not None
                else None
            ),
            "builtin_gem_finder": bool(
                self.cfg.get("builtin_gem_finder", True)
            ),
            "gem_selector_strategy": str(
                self.cfg.get("gem_selector_strategy")
                or "highest_first"
            ),
            "last_selector_count": int(self.last_selector_count),
            "castle_hold_ms": int(
                self.cfg.get("castle_hold_ms") or 900
            ),
            "tree_hold_ms": int(
                self.cfg.get("tree_hold_ms") or 750
            ),
            "reliable_press_hold": bool(
                self.cfg.get("reliable_press_hold", True)
            ),
            "press_hold_ms": int(
                self.cfg.get("press_hold_ms") or 180
            ),
        })
        return data

    def status(self, state: str = "running", note: str = "") -> None:
        self.watchdog.enter(self.last_task)
        payload = dict(
            version=VERSION, state=state, task=self.last_task, actions=self.actions,
            marches_sent=self.marches_sent, goals_done=self.goals_done,
            runtime_seconds=int(time.time()-self.started), note=note,
        )
        payload.update(self._metric_status())
        emit_status(**payload)

    def perform_goal(self, label: str, goal: str, max_steps: int = 12, step_wait: float = 0.75) -> bool:
        if not bool(self.cfg.get("ai_fallback", True)):
            self.last_failure_code = "local_ui_unresolved"
            return False
        self.last_task = label
        self.status(note="starting")
        context = f"loadout={self.cfg.get('loadout_color')}; gather_radius={self.cfg.get('gather_radius')}"
        for step in range(1, max_steps + 1):
            png = self.adb.screenshot()
            ai_t0 = time.monotonic()
            self.metrics.inc("ai_calls")
            previous_task = self.last_task
            self.last_task = "ai_action_wait"
            self.status(note="waiting for optional AI (25-second request limit)")
            try:
                ans = self._vision().next_action(png, goal, context)
            except Exception as exc:
                self.metrics.inc('ai_action_errors')
                self.last_failure_code = 'optional_ai_unavailable'
                log(f"[AI] Optional action unavailable: {type(exc).__name__}: {exc}")
                return False
            finally:
                self.last_task = previous_task
                self.metrics.inc("ai_seconds", int(max(0, time.monotonic() - ai_t0)))
                self.status(note="optional AI request finished" if self.last_failure_code != 'optional_ai_unavailable'
                            else "optional AI unavailable; inspect the current game panel")
            status = str(ans.get("status") or "blocked")
            action = str(ans.get("action") or "none")
            conf = float(ans.get("confidence") or 0)
            reason = str(ans.get("reason") or "")[:180]
            self.last_ai_status = status
            self.last_ai_reason = reason
            self.last_ai_screen = str(ans.get("screen") or "")[:180]
            log(f"[{label.upper()}] step {step}/{max_steps}: {status}/{action} conf={conf:.2f} · {reason}")
            self.status(note=reason)
            if status == "done":
                self.goals_done += 1
                return True
            if status == "blocked":
                return False
            if status == "wait" or action == "wait":
                time.sleep(1.5)
                continue
            if conf < 0.48:
                log(f"[{label.upper()}] Low-confidence action rejected; waiting for a safer frame.")
                time.sleep(1.2)
                continue
            if action == "back":
                self.metrics.inc('ai_back_blocked')
                self.last_failure_code = 'manual_panel_recovery_required'
                log('[SAFETY] AI requested Back; inspect the panel manually')
                return False
            elif action == "tap":
                with Image.open(io.BytesIO(png)) as im:
                    x = int(float(ans.get("x") or 0.5) * im.width)
                    y = int(float(ans.get("y") or 0.5) * im.height)
                self.adb.tap(x, y); self.actions += 1
            elif action == "swipe":
                with Image.open(io.BytesIO(png)) as im:
                    x1 = int(float(ans.get("x") or 0.5) * im.width)
                    y1 = int(float(ans.get("y") or 0.75) * im.height)
                    x2 = int(float(ans.get("x2") or 0.5) * im.width)
                    y2 = int(float(ans.get("y2") or 0.30) * im.height)
                self.adb.swipe(x1, y1, x2, y2, 550); self.actions += 1
            time.sleep(step_wait)
        log(f"[{label.upper()}] Step budget exhausted; will retry later.")
        return False

    @staticmethod
    def _looks_like_title_screen(png: bytes) -> bool:
        """
        Fast local heuristic for the RoK splash/title screen.

        No AI call is needed. The detector looks for:
        - the gold/white RoK logo area in the upper-left
        - the dark lower-center start banner with light text

        This keeps startup fast while avoiding a blind tap during normal gameplay.
        """
        try:
            with Image.open(io.BytesIO(png)) as im:
                src = im.convert("RGB")
                # Normalize work size so thresholds behave the same at different
                # emulator resolutions.
                src.thumbnail((480, 270), Image.Resampling.BILINEAR)
                w, h = src.size
                if w < 160 or h < 90:
                    return False

                def ratios(box):
                    crop = src.crop(box)
                    pixels = list(crop.getdata())
                    if not pixels:
                        return (0.0, 0.0, 0.0)

                    gold = 0
                    dark = 0
                    light_gray = 0

                    for r, gg, b in pixels:
                        avg = (r + gg + b) / 3.0
                        if (
                            r > 115
                            and gg > 55
                            and b < 135
                            and r > gg * 1.04
                            and gg > b * 1.03
                        ):
                            gold += 1

                        if avg < 100:
                            dark += 1

                        if (
                            r > 120
                            and gg > 120
                            and b > 120
                            and max(r, gg, b) - min(r, gg, b) < 55
                        ):
                            light_gray += 1

                    total = float(len(pixels))
                    return (
                        gold / total,
                        dark / total,
                        light_gray / total,
                    )

                upper_left = (
                    int(w * 0.02),
                    int(h * 0.02),
                    int(w * 0.29),
                    int(h * 0.25),
                )
                lower_center = (
                    int(w * 0.34),
                    int(h * 0.76),
                    int(w * 0.66),
                    int(h * 0.91),
                )

                ul_gold, _, ul_light = ratios(upper_left)
                _, lc_dark, lc_light = ratios(lower_center)

                return (
                    ul_gold >= 0.025
                    and (ul_light >= 0.010 or ul_gold >= 0.055)
                    and lc_dark >= 0.55
                    and lc_light >= 0.003
                )
        except Exception:
            return False

    def start_game_gate(self) -> bool:
        """
        Start RoK immediately when the splash screen says "Tap to Start".

        Returns True only when the fast title-screen detector matched and a tap
        was sent.
        """
        try:
            png = self.adb.screenshot()
        except Exception as exc:
            log(f"[START] Screenshot warning: {exc}")
            return False

        if not self._looks_like_title_screen(png):
            return False

        try:
            with Image.open(io.BytesIO(png)) as im:
                # "Tap to Start" is consistently centered in the lower portion
                # of the RoK title screen. Use the screenshot's real dimensions.
                x = int(im.width * 0.50)
                y = int(im.height * 0.845)

            self.last_task = "tap_to_start"
            self.status(note="RoK title screen detected")
            log("[START] RoK title screen detected -> tapping 'Tap to Start'")
            self.adb.tap(x, y)
            self.actions += 1
            time.sleep(2.2)
            return True
        except Exception as exc:
            log(f"[START] Tap-to-start warning: {exc}")
            return False

    def recover_login_gate(self, *, startup: bool = False) -> bool:
        """
        Clear the ordinary RoK saved-account login popup.

        This is intentionally narrow:
        - press Login only for the account that is already selected
        - never open the account selector
        - never choose "Log in to another account"
        - never enter or modify credentials/security settings
        """
        label = "login_recovery"

        # Fast path first: avoid a slow vision round-trip when the normal
        # RoK title screen is visible.
        self.start_game_gate()
        png = self.adb.screenshot()
        if self._best_template_hit(png, ('tree_action_reference', 'leave_city_castle', 'leave_city_map', 'world_search_button')):
            self.last_task = 'game_ready'
            self.status(note='game controls recognized locally; skipping login AI')
            return True

        goal = (
            "Make Rise of Kingdoms ready for normal gameplay. "
            "IMPORTANT: if the Rise of Kingdoms title/splash screen shows "
            "'Tap to Start', press 'Tap to Start' and continue. NEVER return done "
            "while 'Tap to Start' is still visible. "
            "If a centered Login popup is visible and it already shows a saved account "
            "(for example an email/account row) with a large visible Login button, "
            "press ONLY that Login button to continue the already-selected saved account. "
            "Do NOT tap the saved-account row or dropdown arrow. "
            "Do NOT choose 'Log in to another account'. "
            "Do NOT enter, change, or reveal credentials. "
            "Do NOT change account or security settings. "
            "Return done only when 'Tap to Start' is NOT visible, there is no "
            "account-login popup needing action, and the game is loading past the "
            "title screen or normal RoK gameplay UI is visible. "
            "After pressing Login, wait through loading screens and return done once the "
            "login popup is gone and the game is continuing normally. "
            "If the popup requires credentials, account selection, verification, CAPTCHA, "
            "or any other user input, return blocked."
        )

        max_steps = 10 if startup else 6
        step_wait = 1.4 if startup else 1.0

        ok = self.perform_goal(
            label,
            goal,
            max_steps=max_steps,
            step_wait=step_wait,
        )

        if ok:
            log("[LOGIN] Saved-account gate is clear.")
        else:
            log("[LOGIN] Login gate was not cleared automatically.")
        return ok

    def _best_template_hit(
        self,
        png: bytes,
        names: tuple[str, ...],
        *,
        thresholds: dict[str, float] | None = None,
        region: tuple[float, float, float, float] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Return the best local match across several known UI variants."""
        if not self.fast_ui.available:
            return None

        best = None
        thresholds = thresholds or {}

        for name in names:
            hit = self.fast_ui.find(
                png,
                name,
                threshold=max(0.78, thresholds.get(name) or 0.0) if name in {
                    'tree_action_reference', 'leave_city_castle', 'leave_city_map', 'world_search_button'
                } else thresholds.get(name),
                region=(0.0, 0.45, 0.22, 1.0) if name in {
                    'tree_action_reference', 'leave_city_castle', 'leave_city_map', 'world_search_button'
                } else region,
            )
            if hit is None:
                continue
            if best is None or hit["score"] > best[1]["score"]:
                best = (name, hit)

        return best

    def _wait_for_template_any(
        self,
        names: tuple[str, ...],
        *,
        timeout: float,
        thresholds: dict[str, float] | None = None,
        region: tuple[float, float, float, float] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """
        Fast event-driven UI wait.

        ADB screenshots are the pacing mechanism; there is no fixed half-second
        sleep between every known dispatch state.
        """
        deadline = time.monotonic() + max(0.10, timeout)
        attempts = 0

        while time.monotonic() < deadline:
            attempts += 1
            png = self.adb.screenshot()
            self.metrics.inc("turbo_state_scans")
            result = self._best_template_hit(
                png,
                names,
                thresholds=thresholds,
                region=region,
            )
            if result is not None:
                self.metrics.inc("turbo_state_hits")
                return result

            # Tiny yield only; screenshot capture already takes most of the time.
            time.sleep(0.035)

        self.metrics.inc("turbo_state_timeouts")
        return None

    def _press_xy(
        self,
        x: int,
        y: int,
        *,
        hold_ms: int | None = None,
    ) -> None:
        """Reliable short press for LDPlayer/RoK buttons."""
        if not bool(self.cfg.get("reliable_press_hold", True)):
            self.adb.tap(x, y)
            return

        if hold_ms is None:
            hold_ms = int(self.cfg.get("press_hold_ms") or 180)

        hold_ms = max(80, min(600, int(hold_ms)))

        # Zero-distance swipe = touch-down, hold, release.
        self.adb.swipe(
            x,
            y,
            x,
            y,
            hold_ms,
        )
        self.metrics.inc("reliable_holds")

    def _confirm_dispatch(self, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        stable = 0
        while time.monotonic() < deadline:
            png = self.adb.screenshot()
            panel = self._best_template_hit(png, ("new_troop_screen_title",),
                thresholds={"new_troop_screen_title": 0.62}, region=(0.24, 0.00, 0.75, 0.18))
            map_control = self._best_template_hit(png, ("tree_action_reference", "world_search_button"),
                thresholds={"tree_action_reference": 0.78, "world_search_button": 0.86})
            march = self._best_template_hit(png, ('march_button_v2', 'march_button'),
                thresholds={'march_button_v2': .78, 'march_button': .78}, region=(.35,.58,.99,.99))
            stable = stable + 1 if panel is None and march is None and map_control is not None else 0
            if stable >= 2:
                self._dispatch_pending = False
                self._gem_dispatch_owned = False
                self.metrics.inc("dispatch_confirmations")
                return True
            time.sleep(0.08)
        self.metrics.inc("dispatch_unconfirmed")
        self.last_failure_code = "dispatch_not_confirmed"
        self._rescue_after = time.monotonic() + 3
        return False

    def _rescue_ready_march_screen(self) -> bool:
        """
        Global known-state rescue.

        If the bot is sitting on a New Troop screen and MARCH is already ready,
        send it immediately without AI or free-slot reasoning.
        """
        if not self.fast_ui.available or not self._gem_dispatch_owned or self._dispatch_pending or time.monotonic() < self._rescue_after:
            return False

        try:
            png = self.adb.screenshot()

            title = self._best_template_hit(
                png,
                ("new_troop_screen_title",),
                thresholds={
                    "new_troop_screen_title": 0.62,
                },
                region=(0.24, 0.00, 0.75, 0.18),
            )
            if title is None:
                return False

            march = self._best_template_hit(
                png,
                (
                    "march_button_v2",
                    "march_button",
                ),
                thresholds={
                    "march_button_v2": 0.62,
                    "march_button": 0.64,
                },
                region=(0.42, 0.65, 0.99, 0.99),
            )
            if march is None:
                return False

            name, hit = march
            log(
                f"[RESCUE] New Troop + ready MARCH "
                f"({name} {hit['score']:.3f}) -> send now"
            )

            self._dispatch_pending = True
            self._press_xy(
                hit["cx"],
                hit["cy"],
            )
            self.actions += 1
            self.metrics.inc("march_rescue_fires")
            self.metrics.progress()
            self._rescue_after = time.monotonic() + 3
            return self._confirm_dispatch()

        except Exception as exc:
            log(f"[RESCUE] March rescue warning: {exc}")
            return False

    def _tap_template_hit(
        self,
        name: str,
        hit: dict[str, Any],
        *,
        note: str,
    ) -> None:
        log(
            f"[TURBO] {note}: {name} "
            f"score={hit['score']:.3f} "
            f"at ({hit['cx']},{hit['cy']})"
        )
        self._press_xy(hit["cx"], hit["cy"])
        self.actions += 1
        self.metrics.inc("turbo_direct_taps")
        self.metrics.progress()

    def _fast_tap_template(
        self,
        name: str,
        *,
        threshold: float | None = None,
        region: tuple[float, float, float, float] | None = None,
        wait: float = 0.45,
    ) -> bool:
        if not self.fast_ui.available:
            return False

        png = self.adb.screenshot()
        hit = self.fast_ui.find(
            png,
            name,
            threshold=threshold,
            region=region,
        )
        if hit is None:
            self.metrics.inc("template_misses")
            return False

        self.metrics.inc("template_hits")
        self.metrics.progress()
        log(
            f"[FAST] {name} score={hit['score']:.3f} "
            f"at ({hit['cx']},{hit['cy']})"
        )
        self.adb.tap(hit["cx"], hit["cy"])
        self.actions += 1
        time.sleep(wait)
        return True

    def _long_hold(
        self,
        x: int,
        y: int,
        *,
        milliseconds: int,
        label: str,
    ) -> None:
        milliseconds = max(400, min(2000, int(milliseconds)))
        log(
            f"[HOLD] {label} for {milliseconds}ms "
            f"at ({x},{y})"
        )
        self.adb.swipe(
            x,
            y,
            x,
            y,
            milliseconds,
        )
        self.actions += 1
        self.metrics.inc("long_holds")
        self.metrics.progress()

    def _choose_gem_selector_icon(
        self,
        icons: list[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        strategy = str(
            self.cfg.get("gem_selector_strategy")
            or "highest_first"
        ).lower()

        icons = sorted(icons, key=lambda x: x["cx"])

        if strategy == "lowest_first":
            index = 0
        elif strategy == "cycle":
            index = len(icons) - 1 - (self.selector_attempt % len(icons))
        else:
            index = max(0, len(icons) - 1 - self._finder_failures % len(icons))

        index = max(0, min(len(icons) - 1, index))
        return index, icons[index]

    def _dismiss_map_selection(self) -> bool:
        png = self.adb.screenshot()
        if not self._best_template_hit(png, ('tree_action_reference', 'world_search_button')):
            self.metrics.inc('popup_dismiss_unconfirmed')
            return False
        with Image.open(io.BytesIO(png)) as im:
            width, height = im.size
        self.adb.tap(int(width * 0.24), int(height * 0.86))
        self.actions += 1
        self.metrics.inc('map_selection_dismissals')
        return True

    def prepare_builtin_gem_search(self) -> bool:
        # Always publish the exit from this short-lived state, including misses.
        # The supervisor must not charge later map work to the finder.
        self._finder_deadline = min(getattr(self, '_cycle_deadline', 0) or float('inf'), time.monotonic() + 25)
        try:
            return self._prepare_builtin_gem_search_impl()
        finally:
            if getattr(self, 'last_task', '') == 'builtin_gem_finder':
                self.last_task = 'find_gem'
                self.status(note='finder finished; returning to map search')

    def _finder_expired(self) -> bool:
        if time.monotonic() < self._finder_deadline:
            return False
        self.metrics.inc('builtin_budget_exhausted')
        self._finder_after = time.monotonic() + 30
        return True

    def _prepare_builtin_gem_search_impl(self) -> bool:
        if not bool(self.cfg.get("builtin_gem_finder", True)):
            return False

        if time.monotonic() < self._finder_after:
            self.metrics.inc("builtin_circuit_skips")
            return False
        self.last_task = "builtin_gem_finder"
        self.status(note="opening built-in gem finder")

        png = self.adb.screenshot()
        tree = self._best_template_hit(
            png,
            ("tree_action_reference",),
            thresholds={
                "tree_action_reference": 0.62,
            },
        )

        if tree is None:
            self._finder_failures += 1
            self._finder_after = time.monotonic() + min(60, 5 * self._finder_failures)
            self.metrics.inc("builtin_tree_misses")
            log(
                "[FINDER] Tree/search control not visible; "
                "using map-search fallback"
            )
            return False

        if self._finder_expired():
            return False
        _, tree_hit = tree
        tree_hold = max(
            450,
            min(
                1600,
                int(self.cfg.get("tree_hold_ms") or 750),
            ),
        )

        self._long_hold(
            tree_hit["cx"],
            tree_hit["cy"],
            milliseconds=tree_hold,
            label="Tree/Search",
        )
        self.metrics.inc("builtin_finder_opens")
        self._finder_open = True

        deadline = min(self._finder_deadline, time.monotonic() + 1.80)
        icons = []

        while time.monotonic() < deadline:
            selector_png = self.adb.screenshot()
            icons = (
                self.fast_ui.find_gem_selector_icons(
                    selector_png,
                    min_icons=3,
                    max_results=10,
                )
                if self.fast_ui.available
                else []
            )
            if icons:
                break
            time.sleep(0.08)

        if self._finder_expired():
            return False
        if not icons:
            # One bounded retry using a FRESH tree match and a slightly longer hold.
            retry = self._best_template_hit(self.adb.screenshot(), ("tree_action_reference",))
            if retry and not self._finder_expired():
                self._long_hold(retry[1]['cx'], retry[1]['cy'], milliseconds=min(1600, tree_hold + 250), label="Tree retry")
                icons = self.fast_ui.find_gem_selector_icons(self.adb.screenshot(), min_icons=3)
        if not icons:
            self._finder_failures += 1
            self._finder_after = time.monotonic() + min(60, 5 * self._finder_failures)
            self._finder_open = False
            self.metrics.inc("builtin_selector_misses")
            log(
                "[FINDER] Multi-gem selector row not detected; "
                "using map-search fallback"
            )
            return False

        if self._finder_expired():
            return False
        self.last_selector_count = len(icons)
        index, target = self._choose_gem_selector_icon(icons)
        self.selector_attempt += 1

        log(
            f"[FINDER] Detected {len(icons)} gem choices; "
            f"selecting #{index + 1} at "
            f"({target['cx']},{target['cy']})"
        )

        self._press_xy(
            target["cx"],
            target["cy"],
            hold_ms=150,
        )
        self.actions += 1
        self.metrics.inc("builtin_gem_choices")
        self.metrics.progress()

        # Wait for selector disappearance instead of assuming a fixed delay worked.
        deadline = min(self._finder_deadline, time.monotonic() + 2.5)
        while time.monotonic() < deadline:
            if not self.fast_ui.find_gem_selector_icons(self.adb.screenshot(), min_icons=3):
                self._finder_open = False
                self.metrics.inc("builtin_transitions_confirmed")
                break
            time.sleep(0.08)
        else:
            self._finder_failures += 1
            self._finder_after = time.monotonic() + 15
            self.metrics.inc("builtin_transition_failures")
            if not self._finder_expired():
                self._dismiss_map_selection()
            self._finder_open = False
            return False

        self.last_task = "find_gem"
        self.status(
            note=(
                f"finder selected gem "
                f"{index + 1}/{len(icons)}"
            )
        )
        return True

    def ensure_world_map_fast(self) -> bool:
        """
        Leave the city using the current RoK castle button.

        Current UI:
          leave_city_castle = primary

        Older UI:
          leave_city_map = fallback

        The tree icon opens resource search; it does not leave the city.
        """
        self.last_task = "leave_city"
        self.status(note="checking world map / city exit")

        png = self.adb.screenshot()

        if self._best_template_hit(png, ("tree_action_reference", "world_search_button")):
            self.metrics.inc("already_on_world_map")
            return True

        hit = self._best_template_hit(
            png,
            (
                "leave_city_castle",
                "leave_city_map",
            ),
            thresholds={
                "leave_city_castle": 0.64,
                "leave_city_map": 0.68,
            },
        )

        if hit is None:
            # No city-exit icon usually means we are already on the world map.
            return False

        name, match = hit

        log(
            f"[MAP] Leave-city control detected: "
            f"{name} score={match['score']:.3f}"
        )

        if name == "leave_city_castle":
            hold_ms = max(
                500,
                min(
                    1800,
                    int(self.cfg.get("castle_hold_ms") or 900),
                ),
            )
            self._long_hold(
                match["cx"],
                match["cy"],
                milliseconds=hold_ms,
                label="Castle Leave City",
            )
            self.metrics.inc("leave_city_castle_holds")
        else:
            self._press_xy(
                match["cx"],
                match["cy"],
                hold_ms=160,
            )
            self.actions += 1
            self.metrics.inc("leave_city_fallback_presses")
            self.metrics.progress()

        for attempt in range(2):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                frame = self.adb.screenshot()
                if self._best_template_hit(frame, ("tree_action_reference",)):
                    self.metrics.inc("castle_transitions_confirmed")
                    return True
                time.sleep(0.08)
            retry = self._best_template_hit(self.adb.screenshot(), (name,))
            if attempt == 0 and retry:
                self._long_hold(retry[1]['cx'], retry[1]['cy'], milliseconds=min(1800, int(self.cfg.get('castle_hold_ms', 900)) + 250), label="Castle retry")
                self.metrics.inc("castle_hold_retries")
        self.last_failure_code = "castle_transition_unconfirmed"
        self.metrics.inc("castle_transition_failures")
        return False

    def _swipe_search_map(self, *, force_escape: bool = False) -> None:
        radius_value = max(1, min(1000, int(self.cfg.get("gather_radius") or 100)))
        elapsed = max(0.0, time.monotonic() - self.search_started_monotonic)
        decision = self.search_brain.choose_step(
            self.search_point,
            radius_value,
            elapsed=elapsed,
            timeout=self.search_timeout_seconds,
            force_escape=force_escape,
        )

        png = self.adb.screenshot()
        with Image.open(io.BytesIO(png)) as im:
            w, h = im.size
        for candidate in self._scan_gem_candidates(png, threshold=0.57, max_results=12):
            wx, wy = self._gem_world_key(candidate, w, h)
            if self._recently_rejected_gem(wx, wy):
                continue
            verdict = self.fast_ui.classify_gem_candidate(
                png, candidate, mode=str(self.cfg.get('detector_mode') or 'strict')
            ).get('verdict')
            if verdict in {'accept', 'borderline'}:
                self.metrics.inc('swipes_cancelled_for_candidate')
                return

        # Finger moves opposite the desired virtual camera movement.
        start_x = self._search_rng.uniform(0.43, 0.57)
        start_y = self._search_rng.uniform(0.40, 0.60)
        end_x = max(0.12, min(0.88, start_x - decision.dx * 0.56))
        end_y = max(0.12, min(0.88, start_y - decision.dy * 0.56))
        duration = self._search_rng.randint(330, 560)

        self.adb.swipe(
            int(w * start_x), int(h * start_y),
            int(w * end_x), int(h * end_y), duration,
        )
        self.actions += 1
        self.metrics.inc("search_swipes")
        if force_escape:
            self.metrics.inc("escape_swipes")
        self.search_swipe_index += 1
        self.search_point = decision.point
        self.search_brain.record_position(self.search_point)

        distance = math.hypot(self.search_point.x, self.search_point.y)
        coverage = self.search_brain.coverage_percent(radius_value)
        log(
            f"[SEARCH] {decision.reason} swipe #{self.search_swipe_index} "
            f"pos=({self.search_point.x:.2f},{self.search_point.y:.2f}) "
            f"distance={distance:.2f}/{decision.effective_radius:.2f} "
            f"coverage={coverage:.1f}% mode={self.search_brain.mode}"
        )
        time.sleep(self.search_brain.settle_delay())

    def _gem_world_key(self, hit: dict[str, Any], image_width: int, image_height: int) -> tuple[float, float]:
        sx = (float(hit["cx"]) / max(1.0, float(image_width))) - 0.5
        sy = (float(hit["cy"]) / max(1.0, float(image_height))) - 0.5
        return (self.search_point.x + sx * 1.15, self.search_point.y + sy * 1.15)

    def _prune_occupied_gems(self) -> None:
        now = time.monotonic()
        self.occupied_gem_targets = [item for item in self.occupied_gem_targets if item[2] > now]

    def _remember_occupied_gem(self, world_x: float, world_y: float, *, ttl: float | None = None) -> None:
        self._prune_occupied_gems()
        if ttl is None:
            ttl = max(30.0, min(900.0, float(self.cfg.get("occupied_cooldown_seconds") or 180)))
        self.occupied_gem_targets.append((world_x, world_y, time.monotonic() + ttl))
        self.search_brain.record_occupied(world_x, world_y)
        self.metrics.inc("occupied_skipped")
        if len(self.occupied_gem_targets) > 40:
            self.occupied_gem_targets = self.occupied_gem_targets[-40:]

    def _recently_rejected_gem(self, world_x: float, world_y: float) -> bool:
        self._prune_occupied_gems()
        return any(math.hypot(world_x - ox, world_y - oy) <= 0.20 for ox, oy, _ in self.occupied_gem_targets)

    def _selected_gem_has_gather_button(self) -> bool:
        for _ in range(3):
            png = self.adb.screenshot()
            hit = self.fast_ui.find(png, "gather_button", threshold=0.70) if self.fast_ui.available else None
            if hit is not None:
                return True
            time.sleep(0.18)
        return False

    def find_gem_node_fast(self, max_swipes: int | None = None) -> bool:
        self.last_task = "find_gem"
        self.search_timed_out = False
        self.alternate_task_requested = False
        self.search_point = SearchPoint()
        self.search_swipe_index = 0
        self._prune_occupied_gems()
        self.search_brain.set_mode(str(self.cfg.get("search_mode") or "smart"))
        self.search_brain.reset_search()

        radius_value = max(1, min(1000, int(self.cfg.get("gather_radius") or 100)))
        timeout_seconds = max(30, min(900, int(self.cfg.get("gem_search_timeout_seconds") or 240)))
        # Keep the search within the four-minute budget.
        timeout_seconds = min(timeout_seconds, 240)
        self.search_timeout_seconds = float(timeout_seconds)
        self.search_started_monotonic = time.monotonic()
        self.metrics.begin_search()
        deadline = self._cycle_deadline or (self.search_started_monotonic + timeout_seconds)
        next_finder_retry = time.monotonic()
        occupied_skips = 0
        empty_scans = 0
        rescan_same_view = False

        log(
            f"[GEMOPS] Smart free-gem search mode={self.search_brain.mode} "
            f"radius={radius_value} max_time={timeout_seconds}s"
        )

        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - self.search_started_monotonic)
            png = self.adb.screenshot()
            self.metrics.inc("screens_scanned")
            duplicate, duplicate_streak = self.search_brain.observe_view(png)
            if duplicate:
                self.metrics.inc("duplicate_views")

            with Image.open(io.BytesIO(png)) as im:
                image_width, image_height = im.size

            if self._finder_open and self.fast_ui.find_gem_selector_icons(png, min_icons=3):
                self.metrics.inc("selector_ui_rejections")
                if not self._dismiss_map_selection():
                    raise RuntimeError('Finder panel did not close; inspect the game before restarting')
                self._finder_open = False
                continue
            self._finder_open = False
            hits = (
                self._scan_gem_candidates(
                    png,
                    threshold=0.57,
                    max_results=12,
                )
                if self.fast_ui.available
                else []
            )
            self.metrics.inc("gem_candidates_seen", len(hits))

            detector_mode = str(
                self.cfg.get("detector_mode") or "strict"
            ).lower()

            # Multi-signal classification happens BEFORE any candidate is tapped.
            ranked: list[
                tuple[
                    float,
                    dict[str, Any],
                    float,
                    float,
                    dict[str, Any],
                ]
            ] = []

            for hit in hits:
                wx, wy = self._gem_world_key(
                    hit,
                    image_width,
                    image_height,
                )

                if self._recently_rejected_gem(wx, wy):
                    self.metrics.inc("recent_occupied_skips")
                    self.metrics.inc("false_positive_prevented")
                    self._record_candidate(
                        "reject_recent",
                        hit,
                        {},
                        image_width=image_width,
                        image_height=image_height,
                        reason="recently rejected node",
                    )
                    continue

                analysis = self.fast_ui.classify_gem_candidate(
                    png,
                    hit,
                    mode=detector_mode,
                )
                verdict = str(analysis.get("verdict") or "reject")

                if verdict == "occupied":
                    occupied_skips += 1
                    self._remember_occupied_gem(wx, wy)
                    self.metrics.inc("cv_occupied_rejections")
                    self.metrics.inc("false_positive_prevented")
                    self._record_candidate(
                        "occupied",
                        hit,
                        analysis,
                        image_width=image_width,
                        image_height=image_height,
                    )
                    log(
                        "[DETECT] occupied candidate rejected "
                        f"axe={analysis.get('axe',0):.3f} "
                        f"green={analysis.get('green',0):.4f}"
                    )
                    continue

                if verdict == "reject":
                    self.metrics.inc("cv_candidate_rejections")
                    self.metrics.inc("false_positive_prevented")
                    self._record_candidate(
                        "reject_cv",
                        hit,
                        analysis,
                        image_width=image_width,
                        image_height=image_height,
                    )
                    log(
                        "[DETECT] false-positive candidate rejected "
                        f"conf={analysis.get('confidence',0):.3f} "
                        f"reason={analysis.get('reason','')}"
                    )
                    continue

                center_dist = math.hypot(
                    hit["cx"] / image_width - 0.5,
                    hit["cy"] / image_height - 0.5,
                )

                rank_score = (
                    float(analysis.get("confidence") or 0.0) * 8.0
                    + max(0.0, 0.65 - center_dist) * 0.8
                    + (0.6 if verdict == "accept" else -0.6)
                    - abs(float(hit.get("scale", 1)) - float(self.preferred_gem_scale or hit.get("scale", 1))) * 0.2
                )

                ranked.append(
                    (
                        rank_score,
                        hit,
                        wx,
                        wy,
                        analysis,
                    )
                )

            ranked.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            if ranked:
                empty_scans = 0

                for (
                    candidate_index,
                    (
                        rank_score,
                        hit,
                        wx,
                        wy,
                        analysis,
                    ),
                ) in enumerate(ranked[:5], start=1):
                    self.metrics.inc("candidate_checks")

                    stable, second_analysis = (
                        self._temporal_confirm_candidate(
                            hit,
                            detector_mode=detector_mode,
                        )
                    )

                    if not stable:
                        self._remember_occupied_gem(wx, wy, ttl=3)
                        self._record_candidate(
                            "reject_temporal",
                            hit,
                            second_analysis or analysis,
                            image_width=image_width,
                            image_height=image_height,
                            reason="failed two-frame stability/validation",
                        )
                        log(
                            "[DETECT] temporal confirmation rejected "
                            f"candidate {candidate_index}"
                        )
                        continue

                    if second_analysis is not None:
                        # Use the more recent frame confidence conservatively.
                        analysis = {
                            **analysis,
                            "verdict": "borderline" if second_analysis.get("verdict") == "borderline" else analysis.get("verdict"),
                            "confidence": min(
                                float(analysis.get("confidence") or 0.0),
                                float(
                                    second_analysis.get("confidence")
                                    or analysis.get("confidence")
                                    or 0.0
                                ),
                            ),
                        }

                    local_verdict = str(
                        analysis.get("verdict") or "borderline"
                    )

                    if local_verdict == "borderline" and not (
                        self.cfg.get("ai_verify_borderline", True) and self.cfg.get("ai_fallback", True)
                    ):
                        self.metrics.inc("borderline_local_rejections")
                        self._remember_occupied_gem(wx, wy, ttl=8)
                        continue
                    if (
                        local_verdict == "borderline"
                        and bool(
                            self.cfg.get(
                                "ai_verify_borderline",
                                True,
                            )
                        )
                    ):
                        ai_ok, ai_result = self._ai_verify_candidate(
                            png,
                            hit,
                        )
                        if not ai_ok:
                            self._remember_occupied_gem(wx, wy, ttl=8)
                            self._record_candidate(
                                "reject_ai",
                                hit,
                                analysis,
                                image_width=image_width,
                                image_height=image_height,
                                reason=(
                                    f"AI: {ai_result.get('class')} "
                                    f"{float(ai_result.get('confidence') or 0):.2f} "
                                    f"{ai_result.get('reason','')}"
                                ),
                            )
                            log(
                                "[DETECT] borderline candidate rejected by AI: "
                                f"{ai_result.get('class')} "
                                f"conf={float(ai_result.get('confidence') or 0):.2f}"
                            )
                            continue

                        analysis["confidence"] = max(
                            float(analysis.get("confidence") or 0.0),
                            min(
                                1.0,
                                float(
                                    ai_result.get("confidence")
                                    or 0.0
                                ),
                            ),
                        )
                        self.metrics.inc("accepted_after_ai")
                    else:
                        self.metrics.inc("accepted_locally")

                    self._record_candidate(
                        "candidate_ready",
                        hit,
                        analysis,
                        image_width=image_width,
                        image_height=image_height,
                        reason=(
                            f"rank {rank_score:.2f} "
                            f"profile {detector_mode}"
                        ),
                    )

                    log(
                        "[DETECT] candidate passed pre-click validation "
                        f"conf={float(analysis.get('confidence') or 0):.3f} "
                        f"template={float(analysis.get('template') or 0):.3f} "
                        f"hist={float(analysis.get('hist') or 0):.3f} "
                        f"red={float(analysis.get('red_match') or 0):.3f} "
                        f"edge={float(analysis.get('edge') or 0):.3f}"
                    )

                    if time.monotonic() >= deadline:
                        break
                    self.adb.tap(
                        hit["cx"],
                        hit["cy"],
                    )
                    self.actions += 1
                    time.sleep(0.26)

                    if self._selected_gem_has_gather_button():
                        find_seconds = self.metrics.gem_found()
                        self.search_brain.record_free(wx, wy)
                        self.metrics.progress()
                        self.metrics.inc("confirmed_free_gems")
                        self.preferred_gem_scale = float(
                            hit.get("scale")
                            or self.preferred_gem_scale
                            or 1.0
                        )
                        self.metrics.inc("zoom_scale_learned")
                        self._finder_failures = 0
                        self.last_failure_code = ""
                        self._record_candidate(
                            "FREE_GEM",
                            hit,
                            analysis,
                            image_width=image_width,
                            image_height=image_height,
                            reason="GATHER button confirmed",
                        )
                        log(
                            f"[GEMOPS] FREE GEM locked in "
                            f"{find_seconds:.1f}s after "
                            f"{self.search_swipe_index} swipes; "
                            f"coverage="
                            f"{self.search_brain.coverage_percent(radius_value):.1f}%"
                        )
                        return True

                    # Even a candidate that passed visual validation is rejected
                    # if RoK does not expose GATHER after the click.
                    occupied_skips += 1
                    self.metrics.inc("post_click_rejections")
                    self.metrics.inc("false_positive_prevented")
                    self._remember_occupied_gem(wx, wy)
                    self._record_candidate(
                        "reject_no_gather",
                        hit,
                        analysis,
                        image_width=image_width,
                        image_height=image_height,
                        reason="clicked candidate produced no GATHER button",
                    )
                    log(
                        "[DETECT] post-click reject: no GATHER button; "
                        "memorized target"
                    )
                    self._debug_capture(
                        "candidate_no_gather"
                    )
                    try:
                        self._dismiss_map_selection()
                        time.sleep(0.16)
                    except Exception:
                        pass

                    rescan_same_view = True
                    break
            else:
                empty_scans += 1

            coverage = self.search_brain.coverage_percent(radius_value)
            self.status(
                note=(
                    f"GemOps search {elapsed}/{timeout_seconds}s · "
                    f"coverage {coverage:.1f}% · occupied {occupied_skips}"
                )
            )

            if rescan_same_view:
                rescan_same_view = False
                continue

            if time.monotonic() >= next_finder_retry and deadline - time.monotonic() > 12:
                self._finder_failures += 1
                if self.prepare_builtin_gem_search():
                    self.search_point = SearchPoint()
                    self.occupied_gem_targets.clear()
                    self.search_brain.reset_search()
                    self.metrics.inc("adaptive_finder_retries")
                next_finder_retry = time.monotonic() + min(80, 35 + self._finder_failures * 10)
                self.last_task = "find_gem"
                continue

            # Repeated screenshots mean camera motion failed or we are trapped
            # against UI/map geometry. A larger frontier escape fixes that.
            force_escape = duplicate_streak >= 3 or empty_scans >= 7
            if force_escape:
                self.metrics.inc("stuck_recoveries")
                log(
                    f"[WATCHDOG] Repeated/empty view detected "
                    f"(dup={duplicate_streak}, empty={empty_scans}) -> escape swipe"
                )
                empty_scans = 0

            self._swipe_search_map(force_escape=force_escape)

        elapsed = int(time.monotonic() - self.search_started_monotonic)
        self.search_timed_out = True
        self.last_failure_code = "gem_search_timeout"
        self.alternate_task_requested = True
        self.last_task = "alternate_task_pending"
        self.metrics.search_timeout()
        self.metrics.inc("occupied_total_at_timeout", occupied_skips)
        self._debug_capture("gem_search_timeout")
        self.status(
            note=(
                f"no free gem after {elapsed}s · coverage "
                f"{self.search_brain.coverage_percent(radius_value):.1f}% · alternate task"
            )
        )
        log(
            f"[GEMOPS] 4-minute search exhausted: no FREE gem. "
            f"occupied={occupied_skips} coverage={self.search_brain.coverage_percent(radius_value):.1f}% "
            "-> alternate-task hook"
        )
        return False

    def run_alternate_task(self) -> None:
        """
        Placeholder hook for the next task the user wants to add.

        For now, the bot deliberately STOPS gem searching after the four-minute
        timeout and remains alive in alternate_task_pending state. This avoids
        endlessly roaming the map or immediately starting another 4-minute
        search before the alternate task is defined.
        """
        self.last_task = "alternate_task_pending"
        log(
            "[ALT] Alternate-task hook reached. "
            "No alternate task is installed yet; standing by."
        )

        while self.alternate_task_requested:
            self.status(
                note=(
                    "4-minute gem search expired; "
                    "waiting for alternate task implementation"
                )
            )
            time.sleep(5.0)

    @staticmethod
    def _parse_march_counter_text(text: str) -> tuple[int, int] | None:
        """
        Parse RoK march counters such as 0/5, 1/5, 4/6, 7/7.

        Semantics used by this bot:
          X/Y = X marches currently in use out of Y total slots.
          free = Y - X

        Therefore:
          0/5 -> 5 free
          1/5 -> 4 free
          5/5 -> 0 free
        """
        raw = str(text or "")
        for m in re.finditer(r"(?<!\d)([0-7])\s*/\s*([5-7])(?!\d)", raw):
            used = int(m.group(1))
            total = int(m.group(2))
            if 0 <= used <= total:
                return used, total
        return None

    def _update_march_capacity_from_ai(self) -> tuple[int, int] | None:
        combined = (
            f"{self.last_ai_screen} {self.last_ai_reason}"
        )
        parsed = self._parse_march_counter_text(combined)
        if parsed is None:
            return None

        used, total = parsed
        self.last_march_used = used
        self.last_march_total = total
        free = total - used
        log(
            f"[MARCH] Counter read as {used}/{total}: "
            f"{free} free march slot(s)"
        )
        return parsed

    def _confirmed_marches_full(self) -> bool:
        parsed = self._update_march_capacity_from_ai()
        if parsed is None:
            return False
        used, total = parsed
        return used >= total

    def _confirmed_free_march(self) -> bool:
        parsed = self._update_march_capacity_from_ai()
        if parsed is None:
            return False
        used, total = parsed
        return used < total

    def _open_new_troop_with_capacity_logic(self) -> bool:
        """
        Recover when the New Troop image template misses.

        X/Y means X used out of Y total. So 0/5 is five free slots,
        not zero free slots. AI is explicitly forbidden from marking X/Y full
        unless X == Y.
        """
        goal = (
            "You are handling Rise of Kingdoms march capacity after selecting a "
            "neutral GEM DEPOSIT and pressing Gather. Read any visible march "
            "counter using this EXACT rule: X/Y means X marches are CURRENTLY IN "
            "USE out of Y TOTAL slots, so FREE = Y-X. Examples: 0/5 means FIVE "
            "free marches; 1/5 means FOUR free; 4/5 means ONE free; 5/5 means "
            "ZERO free. Same rule for /6 and /7. If X<Y, DO NOT return blocked "
            "for lack of marches. Press New Troop or otherwise proceed into the "
            "unused/free march slot. Only return blocked for march capacity when "
            "X==Y. Never attack or spend gems."
        )

        ok = self.perform_goal(
            "new_troop_capacity",
            goal,
            max_steps=5,
            step_wait=0.25,
        )

        if ok:
            self.last_failure_code = ""
            self._update_march_capacity_from_ai()
            return True

        if self._confirmed_marches_full():
            self.last_failure_code = "marches_full"
            used = self.last_march_used
            total = self.last_march_total
            log(f"[MARCH] Confirmed full: {used}/{total}; no free slots")
            return False

        if self._confirmed_free_march():
            used = self.last_march_used
            total = self.last_march_total
            free = total - used
            log(
                f"[MARCH] AI stopped despite {used}/{total}; "
                f"{free} slot(s) are free. Retrying New Troop immediately."
            )

            retry_goal = (
                f"The march counter is {used}/{total}. That means {free} FREE "
                "march slot(s). Do NOT return blocked. Press New Troop and select "
                "one unused/free march slot, then stop when the MARCH button is "
                "ready. Never attack."
            )
            retry = self.perform_goal(
                "new_troop_retry",
                retry_goal,
                max_steps=5,
                step_wait=0.20,
            )
            if retry:
                self.last_failure_code = ""
                return True

        self.last_failure_code = "new_troop_ui_miss"
        return False

    def select_free_march_slot_if_needed(self) -> bool:
        png = self.adb.screenshot()
        hit = (
            self.fast_ui.find(
                png,
                "march_button",
                threshold=0.72,
            )
            if self.fast_ui.available
            else None
        )

        if hit is not None:
            self.last_failure_code = ""
            log("[MARCH] New Troop already has an unused march slot ready")
            return True

        goal = (
            "You are on the Rise of Kingdoms New Troop/troop dispatch screen. "
            "Read any visible march counter with this EXACT meaning: X/Y = X "
            "marches currently IN USE out of Y TOTAL. FREE = Y-X. Therefore 0/5 "
            "means 5 FREE, 1/5 means 4 FREE, 4/5 means 1 FREE, and 5/5 means "
            "0 FREE. The same rule applies to /6 and /7. Select ONE UNUSED/FREE "
            "march slot. If X<Y, you MUST NOT return blocked for march capacity. "
            "Only return blocked for capacity when X==Y. If a free slot is "
            "already selected and MARCH is ready, return done. Do not choose a "
            "march already marching, gathering, fighting, or returning. Never "
            "attack or spend gems."
        )

        ok = self.perform_goal(
            "free_march_slot",
            goal,
            max_steps=5,
            step_wait=0.22,
        )

        if ok:
            self.last_failure_code = ""
            self._update_march_capacity_from_ai()
            return True

        if self._confirmed_marches_full():
            self.last_failure_code = "marches_full"
            used = self.last_march_used
            total = self.last_march_total
            log(f"[MARCH] All march slots confirmed in use: {used}/{total}")
            return False

        if self._confirmed_free_march():
            used = self.last_march_used
            total = self.last_march_total
            free = total - used
            log(
                f"[MARCH] Counter {used}/{total} means {free} free. "
                "Forcing one more free-slot selection attempt."
            )
            retry = self.perform_goal(
                "free_march_force",
                f"Counter is {used}/{total}, so {free} march slot(s) are FREE. "
                "Select one unused slot now and make the MARCH button ready. "
                "Do not return blocked unless the counter changes to "
                f"{total}/{total}.",
                max_steps=5,
                step_wait=0.20,
            )
            if retry:
                self.last_failure_code = ""
                return True

        self.last_failure_code = "free_slot_ui_miss"
        return False

    def dispatch_selected_gem_fast(self, *, resume: bool = False) -> bool:
        """
        Turbo known-state dispatch:

          GATHER -> New Troop -> MARCH

        The normal path is entirely local computer vision. AI/capacity recovery
        is reserved for screens that genuinely do not match known states.
        """
        dispatch_t0 = time.monotonic()
        self._gem_dispatch_owned = True
        turbo = bool(self.cfg.get("turbo_dispatch", True))

        if not resume:
            self.last_task = "gather_button"
            self.status(note="Turbo dispatch: Gather")

            # GATHER is already expected because free-gem validation required it.
            gather = self._wait_for_template_any(
                ("gather_button",),
                timeout=1.15 if turbo else 1.8,
                thresholds={"gather_button": 0.70},
            )

            if gather is None:
                self.last_failure_code = "gather_ui_miss"
                self.metrics.inc("turbo_fallbacks")
                log("[TURBO] GATHER not resolved locally; fallback")
                if not self.perform_goal(
                    "gather_button",
                    "A neutral FREE GEM DEPOSIT is selected. Press the blue GATHER "
                    "button immediately. Do not attack and do not select another resource.",
                    max_steps=2,
                    step_wait=0.12,
                ):
                    return False
            else:
                self._tap_template_hit(
                    gather[0],
                    gather[1],
                    note="GATHER",
                )

        self.last_task = "new_troop"
        self.status(note="Turbo dispatch: waiting for New Troop")

        # This includes the original button AND the exact tooltip/button state
        # for the current capacity panel.
        new_troop = self._wait_for_template_any(
            (
                "new_troop_button_v2",
                "new_troop_button",
            ),
            timeout=6.0,
            thresholds={
                "new_troop_button_v2": 0.60,
                "new_troop_button": 0.67,
            },
            # New Troop normally appears on the right side of the screen.
            region=(0.56, 0.08, 0.96, 0.60),
        )

        if new_troop is None:
            self.metrics.inc("turbo_fallbacks")
            log(
                "[TURBO] New Troop known-state match missed. "
                "Checking capacity/UI fallback without assuming full."
            )
            if not self._open_new_troop_with_capacity_logic():
                if self.last_failure_code == "marches_full":
                    log("[MARCH] Capacity is actually full")
                else:
                    self._debug_capture("turbo_new_troop_miss")
                return False
        else:
            self._tap_template_hit(
                new_troop[0],
                new_troop[1],
                note="NEW TROOP",
            )

        self.last_task = "march"
        self.status(note="Turbo dispatch: waiting for March")

        # Most free New Troop slots immediately load a usable troop and expose
        # MARCH. If so, do NOT call AI/free-slot selection at all.
        march = self._wait_for_template_any(
            (
                "march_button_v2",
                "march_button",
            ),
            timeout=1.45 if turbo else 2.8,
            thresholds={
                "march_button_v2": 0.62,
                "march_button": 0.65,
            },
            region=(0.35, 0.58, 0.99, 0.99),
        )

        if march is None:
            self.metrics.inc("turbo_slot_fallbacks")
            log(
                "[TURBO] MARCH was not immediately ready. "
                "Only now checking free-slot state."
            )

            if not self.select_free_march_slot_if_needed():
                if self.last_failure_code != "marches_full":
                    self._debug_capture("turbo_slot_miss")
                return False

            march = self._wait_for_template_any(
                (
                    "march_button_v2",
                    "march_button",
                ),
                timeout=1.20,
                thresholds={
                    "march_button_v2": 0.60,
                    "march_button": 0.64,
                },
                region=(0.35, 0.58, 0.99, 0.99),
            )

        if march is None:
            self.last_failure_code = "march_button_ui_miss"
            self.metrics.inc("turbo_fallbacks")
            log("[TURBO] MARCH local state missed; final short AI fallback")
            self._dispatch_pending = bool(self.cfg.get("ai_fallback", True))
            if not self.perform_goal(
                "march_button",
                "A FREE troop is prepared for the already-selected neutral GEM "
                "DEPOSIT. Press MARCH immediately. Return done after it is sent. "
                "Never attack.",
                max_steps=2,
                step_wait=0.12,
            ):
                return False
        else:
            self._dispatch_pending = True
            self._tap_template_hit(
                march[0],
                march[1],
                note="MARCH",
            )
            # Do not burn another long vision pass just to verify a normal send.
            time.sleep(0.16)

        if not self._confirm_dispatch():
            return False

        dispatch_seconds = max(
            0.0,
            time.monotonic() - dispatch_t0,
        )

        self.last_failure_code = ""
        self.marches_sent += 1
        self.metrics.inc("marches_dispatched")
        self.metrics.inc("dispatch_count")
        self.metrics.timing("dispatch", dispatch_seconds)
        self.metrics.inc(
            "dispatch_ms_total",
            int(dispatch_seconds * 1000),
        )
        self.metrics.inc("turbo_dispatch_success")
        self.metrics.progress()
        self.goals_done += 1
        self.status(
            note=(
                f"gem march dispatched in "
                f"{dispatch_seconds:.2f}s"
            )
        )
        log(
            f"[TURBO] Dispatch complete in "
            f"{dispatch_seconds:.2f}s; "
            f"session total={self.marches_sent}"
        )
        return True

    def gather_one_fast(self) -> bool:
        """
        One complete fast cycle:
        leave city -> find gem -> Gather -> New Troop -> unused slot -> March
        """
        self._cycle_deadline = time.monotonic() + 240
        self.last_failure_code = ""
        self.ensure_world_map_fast()
        if self.last_failure_code == "castle_transition_unconfirmed":
            return False
        self.occupied_gem_targets.clear()  # Camera origin changes on finder/recenter.

        if not self.find_gem_node_fast():
            if self.search_timed_out:
                # The four-minute search budget is authoritative. Do not spend
                # another minute in AI search after it expires.
                return False

            log("[GEM] Random search ended without a gem")
            return False

        return self.dispatch_selected_gem_fast()

    def gather_one(self) -> bool:
        # Finish a ready New Troop/MARCH state before starting another search.
        if self._rescue_ready_march_screen():
            self.last_failure_code = ""
            self.marches_sent += 1
            self.metrics.inc("marches_dispatched")
            self.metrics.inc("march_rescue_dispatches")
            self.goals_done += 1
            self.status(note="ready MARCH rescued")
            return True

        if self._gem_dispatch_owned and not self._dispatch_pending:
            png = self.adb.screenshot()
            new_troop = self._best_template_hit(png, ('new_troop_button_v2', 'new_troop_button'),
                thresholds={'new_troop_button_v2': .78, 'new_troop_button': .78}, region=(.56,.08,.96,.60))
            if new_troop:
                self.metrics.inc('new_troop_resumes')
                self.last_failure_code = ''
                return self.dispatch_selected_gem_fast(resume=True)

        if self._dispatch_pending:
            # An ambiguous send must not result in another immediate dispatch.
            if self._confirm_dispatch(timeout=1.0):
                self.marches_sent += 1
                self.metrics.inc("marches_dispatched")
                self.goals_done += 1
                return True
            self.last_failure_code = "dispatch_ambiguous"
            self.status(state="blocked", note="Dispatch not confirmed; inspect Live Ops and restart the mission")
            raise RuntimeError("Ambiguous dispatch: mission stopped to prevent duplicate marches")
        return self.gather_one_fast()

    def bring_home(self) -> None:
        self.perform_goal(
            "recall",
            "Return to the world map and recall only MY marches that are currently "
            "gathering neutral GEM DEPOSITS. Never recall rallies, combat marches, "
            "reinforcements, or other resource marches. Return done when no eligible "
            "gem-gathering marches remain or all are returning.",
            12,
        )

    def run(self) -> int:
        limit_h = max(1, min(24, int(self.cfg.get("daily_runtime_limit") or 24)))
        end = self.started + limit_h * 3600
        retry_base = max(20, min(900, int(self.cfg.get("retry_seconds") or 90)))

        log(f"[GEMOPS] Kingdom Services GemOps v{VERSION} · BUILT-IN GEM FINDER + TURBO")
        log(f"[GEMOPS] local templates={len(self.fast_ui.templates)} AI=lazy search={self.search_brain.mode} detector={self.cfg.get('detector_mode','strict')}")
        log(
            f"[GEM] gems-only marches={self.cfg.get('marches_to_send')} "
            f"radius={self.cfg.get('gather_radius')} device={self.adb.device}"
        )
        self.last_task = "starting"
        self.status(note="gem bot online")

        # Start the game immediately if RoK is sitting at "Tap to Start".
        # This is a fast local image check, not an AI call.
        self.start_game_gate()

        # Clear the ordinary saved-account Login popup before starting the
        # gathering loop. This does not select/change accounts or enter secrets.
        try:
            self.recover_login_gate(startup=True)
        except Exception as exc:
            log(f"[LOGIN] Startup recovery warning: {exc}")

        idle_rounds = 0
        try:
            while time.time() < end:
                auto_fill = bool(self.cfg.get("auto_fill_all_marches", True))
                if auto_fill:
                    # Keep going until RoK stops offering New Troop/free march.
                    # Seven is only a safety ceiling; 5- and 6-march accounts stop
                    # naturally when their free slots are exhausted.
                    target = 7
                else:
                    target = max(
                        1,
                        min(7, int(self.cfg.get("marches_to_send") or 7)),
                    )

                sent_this_round = 0
                transient_failures = 0
                app_was_recovered = False

                log(
                    f"[MARCH] Fill mode={'AUTO 5/6/7' if auto_fill else target}; "
                    "continuing until every available march is filled"
                )

                while sent_this_round < target and time.time() < end:
                    app_was_recovered = False

                    # If the prior step already reached a ready MARCH screen,
                    # finish it immediately before doing anything else.
                    if self._rescue_ready_march_screen():
                        sent_this_round += 1
                        self.marches_sent += 1
                        self.metrics.inc("marches_dispatched")
                        self.metrics.inc("march_rescue_dispatches")
                        self.goals_done += 1
                        self.last_failure_code = ""
                        self.last_task = "find_gem"
                        self.status(
                            note=(
                                f"rescue march sent "
                                f"{sent_this_round}/{target}; "
                                "searching next gem"
                            )
                        )
                        time.sleep(0.18)
                        continue

                    # Only do expensive login/start recovery when RoK actually
                    # lost foreground focus. Do NOT run it between normal marches.
                    try:
                        if not self.adb.rok_foreground():
                            log("[ROK] RoK lost foreground; restoring app")
                            self.adb.ensure_rok()
                            time.sleep(0.8)
                            app_was_recovered = True
                    except Exception as exc:
                        log(f"[ROK] Foreground recovery warning: {exc}")

                    if app_was_recovered:
                        # A real app restore can land on Tap to Start or Login.
                        try:
                            if self.start_game_gate():
                                log("[START] Reconnect title screen cleared")
                            self.recover_login_gate(startup=False)
                        except Exception as exc:
                            log(f"[LOGIN] Reconnect recovery warning: {exc}")

                    # Normal path: stay on map and keep filling marches.
                    if self.gather_one():
                        sent_this_round += 1
                        transient_failures = 0
                        idle_rounds = 0
                        self.last_task = "find_gem"
                        self.status(
                            note=(
                                f"gem march {sent_this_round}/{target} sent; "
                                "searching for next gem"
                            )
                        )
                        log(
                            f"[LOOP] March sent ({sent_this_round}/{target}). "
                            "Continuing directly to next gem."
                        )
                        time.sleep(0.30)
                        continue

                    if self.alternate_task_requested:
                        self.run_alternate_task()
                        break

                    if self.last_failure_code == "marches_full":
                        self._gem_dispatch_owned = False
                        self._dispatch_pending = False
                        used = self.last_march_used
                        total = self.last_march_total
                        log(
                            f"[LOOP] March capacity is actually full "
                            f"({used}/{total}); fill loop complete."
                        )
                        break

                    # A button/template/AI miss is not a reason to end the whole
                    # fill round. Retry the flow quickly and keep trying.
                    transient_failures += 1
                    if transient_failures >= 12:
                        self.status(state="blocked", note="Recovery budget exhausted; inspect Live Ops")
                        raise RuntimeError("12 consecutive flow failures; mission stopped for inspection")
                    retry_wait = min(5.0, 0.8 + transient_failures * 0.55)

                    log(
                        f"[LOOP] Temporary gem-flow failure "
                        f"({self.last_failure_code or 'unknown'}). "
                        f"Keeping fill loop alive; retry #{transient_failures} "
                        f"in {retry_wait:.1f}s."
                    )
                    self.last_task = "gem_flow_recovery"
                    self.status(
                        note=(
                            f"temporary UI miss; continuing fill loop "
                            f"(retry {transient_failures})"
                        )
                    )
                    time.sleep(retry_wait)

                    # Recovery never sends Android Back: it can open Exit Game.
                    if transient_failures % 4 == 0:
                        self._dismiss_map_selection()

                if sent_this_round:
                    idle_rounds = 0
                    log(
                        f"[GEM] Continuous fill dispatched "
                        f"{sent_this_round} gem march(es); total={self.marches_sent}"
                    )

                    if self.last_failure_code == "marches_full":
                        self._gem_dispatch_owned = False
                        self._dispatch_pending = False
                        used = self.last_march_used
                        total = self.last_march_total
                        wait = min(900, retry_base)
                        self.last_task = "waiting_for_marches"
                        self.status(
                            note=f"all marches full {used}/{total}; retry in {wait}s"
                        )
                        log(
                            f"[MARCH] All available marches filled "
                            f"({used}/{total}). Retrying in {wait}s."
                        )
                        time.sleep(wait)
                    elif not self.alternate_task_requested:
                        # If we hit the safety ceiling (normally 7) without a
                        # confirmed full counter, do a short re-check rather than
                        # pretending the bot is restarting.
                        self.last_task = "march_fill_complete"
                        self.status(
                            note=(
                                f"{sent_this_round} march(es) sent; "
                                "checking capacity again shortly"
                            )
                        )
                        time.sleep(3.0)
                else:
                    if self.alternate_task_requested:
                        # run_alternate_task() normally holds here until stopped
                        # or until we add the alternate task implementation.
                        continue

                    idle_rounds += 1

                    if self.last_failure_code == "marches_full":
                        wait = min(900, retry_base + min(5, idle_rounds - 1) * 30)
                        used = self.last_march_used
                        total = self.last_march_total
                        log(
                            f"[MARCH] Capacity confirmed full "
                            f"({used}/{total}). Retrying in {wait}s."
                        )
                        self.last_task = "waiting_for_marches"
                        self.status(
                            note=f"marches full {used}/{total}; retry in {wait}s"
                        )
                        time.sleep(wait)
                    else:
                        # A template/AI miss is not proof that marches are full.
                        # Stay in the gem flow; do not fall back to "starting".
                        short_wait = min(6, 1 + idle_rounds)
                        log(
                            f"[GEM] No march sent this pass "
                            f"({self.last_failure_code or 'unknown'}), "
                            "but capacity is not confirmed full. "
                            f"Continuing gem loop in {short_wait}s."
                        )
                        self.last_task = "find_gem"
                        self.status(
                            note=(
                                f"continuing gem search; quick retry in "
                                f"{short_wait}s"
                            )
                        )
                        time.sleep(short_wait)

        except KeyboardInterrupt:
            log("[STOP] Gem Bot interrupted")
        finally:
            if self.cfg.get("bring_marches_home"):
                try:
                    log("[STOP] Recalling eligible gem-gathering marches...")
                    self.bring_home()
                except Exception as exc:
                    log(f"[STOP] Recall warning: {exc}")

            self.last_task = "stopped"
            self.status(state="stopped", note="gem bot stopped")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to JSON gem-bot config")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    try:
        return GemBot(cfg).run()
    except Exception as exc:
        log(f"[ERROR] {type(exc).__name__}: {exc}")
        emit_status(state="failed", task="gem_error", actions=0, marches_sent=0, goals_done=0, runtime_seconds=0, note=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
