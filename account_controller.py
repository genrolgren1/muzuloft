from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from PIL import Image

from gem_bot import ADB, Vision, OLLAMA, MODEL
from ldplayer_backend import resolve_adb_serial

VERSION = "3.5.1"
RESULT_PREFIX = "@@ACCOUNT_RESULT@@"
PROGRESS_PREFIX = "@@ACCOUNT_PROGRESS@@"


def emit_progress(
    stage: str,
    message: str,
    *,
    method: str = "",
    screen: str = "",
) -> None:
    print(
        PROGRESS_PREFIX
        + json.dumps(
            {
                "stage": str(stage)[:64],
                "message": str(message)[:350],
                "method": str(method)[:64],
                "screen": str(screen)[:120],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def emit_result(ok: bool, message: str, *, blocked: bool = False) -> None:
    print(
        RESULT_PREFIX
        + json.dumps(
            {
                "ok": bool(ok),
                "blocked": bool(blocked),
                "message": str(message)[:500],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _host_target(adb_obj: ADB) -> tuple[str, str]:
    adb_exe = adb_obj.adb
    if adb_obj.ld_index is not None:
        serial = resolve_adb_serial(adb_obj.ld_index, timeout=30)
    else:
        serial = adb_obj.device
    return adb_exe, serial


def _run_adb(
    adb_obj: ADB,
    *args: str,
    timeout: int = 30,
    check: bool = False,
) -> subprocess.CompletedProcess:
    adb_exe, serial = _host_target(adb_obj)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cp = subprocess.run(
        [adb_exe, "-s", serial, *[str(x) for x in args]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        creationflags=flags,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            (cp.stdout or "").strip()
            or f"ADB command failed: {' '.join(str(x) for x in args)}"
        )
    return cp


def _interactive_shell(
    adb_obj: ADB,
    commands: list[str],
    timeout: int = 45,
) -> None:
    """
    Feed private typing commands over stdin so credential values are not placed
    in the host process argv.
    """
    adb_exe, serial = _host_target(adb_obj)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    proc = subprocess.Popen(
        [adb_exe, "-s", serial, "shell"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=flags,
    )

    script = "\n".join(commands + ["exit", ""])
    try:
        proc.communicate(script, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("Android text entry timed out")

    if proc.returncode not in (0, None):
        raise RuntimeError("Android text entry failed")


def _input_payload(text: str) -> str:
    clean = str(text).replace("\r", "").replace("\n", "")
    clean = clean.replace(" ", "%s")
    return shlex.quote(clean)


def public_type(adb_obj: ADB, text: str, *, clear_first: bool = True) -> None:
    """
    Reliable direct ADB text entry for non-secret values such as email/usernames.

    Unlike private_type(), this uses adb shell input text with separate argv
    fields so characters common in email addresses (@ . _ - +) are not lost by
    our interactive-shell quoting layer.
    """
    value = str(text).replace("\r", "").replace("\n", "")
    if not value:
        raise RuntimeError("Required text value is empty")
    if len(value) > 512:
        raise RuntimeError("Text value is too long")

    if clear_first:
        # Clear in ONE persistent adb-shell process instead of launching a new
        # host adb.exe process for every DEL key.
        clear_commands = ["input keyevent KEYCODE_MOVE_END"]
        clear_commands.extend(["input keyevent KEYCODE_DEL"] * 96)
        _interactive_shell(
            adb_obj,
            clear_commands,
            timeout=20,
        )

    # Android input uses %s for spaces. Email addresses normally have none,
    # but preserve expected behavior for general username strings.
    payload = value.replace(" ", "%s")
    cp = _run_adb(
        adb_obj,
        "shell",
        "input",
        "text",
        payload,
        timeout=20,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError("Android email/username text entry failed")


def private_type(adb_obj: ADB, text: str, *, clear_first: bool = True) -> None:
    value = str(text)
    if not value:
        raise RuntimeError("Required text value is empty")
    if len(value) > 512:
        raise RuntimeError("Text value is too long")

    commands: list[str] = []
    if clear_first:
        commands.append("input keyevent KEYCODE_MOVE_END")
        commands.extend(["input keyevent KEYCODE_DEL"] * 128)

    commands.append(f"input text {_input_payload(value)}")
    _interactive_shell(adb_obj, commands)


# ---------------------------------------------------------------------------
# Direct Android UI layer
# ---------------------------------------------------------------------------

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    m = BOUNDS_RE.fullmatch(str(value or "").strip())
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def ui_dump(adb_obj: ADB) -> list[dict[str, Any]]:
    """
    Ask Android accessibility/UIAutomator for the current visible tree.

    RoK sometimes custom-draws controls, so an empty/incomplete tree is normal;
    the controller falls back to vision AI in that case.
    """
    remote = "/sdcard/window_dump_ks.xml"
    _run_adb(
        adb_obj,
        "shell",
        "uiautomator",
        "dump",
        "--compressed",
        remote,
        timeout=18,
        check=False,
    )
    cp = _run_adb(
        adb_obj,
        "exec-out",
        "cat",
        remote,
        timeout=15,
        check=False,
    )
    text = (cp.stdout or "").strip()
    if not text.startswith("<"):
        return []

    try:
        root = ET.fromstring(text)
    except Exception:
        return []

    nodes: list[dict[str, Any]] = []
    for node in root.iter("node"):
        bounds = _parse_bounds(node.attrib.get("bounds", ""))
        if not bounds:
            continue
        nodes.append(
            {
                "text": str(node.attrib.get("text") or "").strip(),
                "desc": str(node.attrib.get("content-desc") or "").strip(),
                "resource": str(node.attrib.get("resource-id") or "").strip(),
                "class": str(node.attrib.get("class") or "").strip(),
                "clickable": str(node.attrib.get("clickable") or "").lower() == "true",
                "enabled": str(node.attrib.get("enabled") or "").lower() != "false",
                "bounds": bounds,
            }
        )
    return nodes


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def find_ui_text(
    adb_obj: ADB,
    wanted: str,
    *,
    exact: bool = True,
) -> dict[str, Any] | None:
    target = _norm(wanted)
    if not target:
        return None

    best: dict[str, Any] | None = None
    best_score = -1

    for node in ui_dump(adb_obj):
        if not node["enabled"]:
            continue

        candidates = [node["text"], node["desc"]]
        for raw in candidates:
            value = _norm(raw)
            if not value:
                continue

            matched = value == target if exact else target in value
            if not matched:
                continue

            score = 100 if value == target else 70
            if node["clickable"]:
                score += 20
            if node["text"]:
                score += 5

            if score > best_score:
                best = node
                best_score = score

    return best


def tap_node(adb_obj: ADB, node: dict[str, Any]) -> None:
    x1, y1, x2, y2 = node["bounds"]
    adb_obj.tap((x1 + x2) // 2, (y1 + y2) // 2)


def direct_tap_text(
    adb_obj: ADB,
    wanted: str,
    *,
    exact: bool = True,
    wait_after: float = 0.8,
) -> bool:
    node = find_ui_text(adb_obj, wanted, exact=exact)
    if not node:
        return False

    emit_progress(
        "direct_tap",
        f"Tapping visible Android control: {wanted}",
        method="uiautomator",
    )
    tap_node(adb_obj, node)
    time.sleep(wait_after)
    return True


def find_editable_field(
    adb_obj: ADB,
    *,
    keywords: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """
    Find the most likely enabled text-entry node from Android's UI tree.
    """
    normalized_keywords = tuple(_norm(x) for x in keywords if str(x).strip())
    best: dict[str, Any] | None = None
    best_score = -1

    for node in ui_dump(adb_obj):
        if not node["enabled"]:
            continue

        cls = _norm(node.get("class", ""))
        text_blob = " ".join(
            [
                _norm(node.get("text", "")),
                _norm(node.get("desc", "")),
                _norm(node.get("resource", "")),
            ]
        )

        keyword_match = (
            bool(normalized_keywords)
            and any(k in text_blob for k in normalized_keywords)
        )

        if normalized_keywords and not keyword_match:
            continue

        score = 0
        if "edittext" in cls:
            score += 80
        if keyword_match:
            score += 100
        if node.get("clickable"):
            score += 10

        if score > best_score and score >= 80:
            best = node
            best_score = score

    return best


def direct_type_email(adb_obj: ADB, email: str) -> bool:
    """
    Focus an Android-exposed Email Address field and type the exact web-supplied
    email into the emulator.
    """
    node = find_editable_field(
        adb_obj,
        keywords=("email", "email address", "username"),
    )
    if not node:
        return False

    emit_progress(
        "direct_email",
        "Focusing Email Address field",
        method="uiautomator",
    )
    tap_node(adb_obj, node)
    time.sleep(0.25)

    emit_progress(
        "typing_email",
        "Typing the web-supplied email into LDPlayer",
        method="adb-text",
    )
    public_type(
        adb_obj,
        email,
        clear_first=True,
    )
    time.sleep(0.6)
    return True


def direct_type_verification_code(adb_obj: ADB, code: str) -> bool:
    """Type the user-supplied email verification code into the exposed code box."""
    node = find_editable_field(
        adb_obj,
        keywords=(
            "verification code",
            "verify code",
            "email code",
            "code",
        ),
    )
    if not node:
        return False

    emit_progress(
        "direct_code",
        "Focusing verification-code field",
        method="uiautomator",
    )
    tap_node(adb_obj, node)
    time.sleep(0.12)

    emit_progress(
        "typing_code",
        "Typing the supplied verification code into LDPlayer",
        method="adb-private",
    )
    private_type(
        adb_obj,
        code,
        clear_first=True,
    )
    time.sleep(0.15)
    return True


def direct_submit_verification_code(adb_obj: ADB) -> bool:
    """Immediately press the exact Verification Code Login submit button."""
    node = find_ui_text(
        adb_obj,
        "Verification Code Login",
        exact=True,
    )
    if not node:
        return False

    emit_progress(
        "submit_code",
        "Pressing Verification Code Login",
        method="uiautomator",
    )
    tap_node(adb_obj, node)
    time.sleep(0.55)
    return True


def direct_tap_verification_code_login(adb_obj: ADB) -> bool:
    """Choose RoK email verification-code login when explicitly requested."""
    node = find_ui_text(adb_obj, "Verification Code Login", exact=True)
    if not node:
        return False
    emit_progress(
        "direct_tap",
        "Switching login method to Verification Code Login",
        method="uiautomator",
    )
    tap_node(adb_obj, node)
    time.sleep(0.9)
    return True


def direct_tap_password_login(adb_obj: ADB) -> bool:
    """
    Handle RoK's login-method chooser.

    If the screen offers both "Verification Code Login" and "Password Login",
    choose the exact "Password Login" control before credential entry.
    """
    node = find_ui_text(
        adb_obj,
        "Password Login",
        exact=True,
    )
    if not node:
        return False

    emit_progress(
        "direct_tap",
        "Switching login method to Password Login",
        method="uiautomator",
    )
    tap_node(adb_obj, node)
    time.sleep(0.9)
    return True


def direct_tap_login(adb_obj: ADB) -> bool:
    """
    Tap the normal Login button only when Android exposes it as a UI node.

    Custom-drawn RoK screens will fall back to AI.
    """
    nodes = ui_dump(adb_obj)
    candidates: list[dict[str, Any]] = []

    for node in nodes:
        text = _norm(node["text"] or node["desc"])
        if text == "login" and node["enabled"]:
            candidates.append(node)

    if not candidates:
        return False

    # Prefer the widest clickable Login node, which is normally the large red
    # login button instead of a title/label.
    candidates.sort(
        key=lambda n: (
            1 if n["clickable"] else 0,
            (n["bounds"][2] - n["bounds"][0]),
            (n["bounds"][3] - n["bounds"][1]),
        ),
        reverse=True,
    )

    emit_progress(
        "direct_tap",
        "Pressing the visible Login button",
        method="uiautomator",
    )
    tap_node(adb_obj, candidates[0])
    time.sleep(1.0)
    return True


# ---------------------------------------------------------------------------
# Vision fallback
# ---------------------------------------------------------------------------

class AccountVision(Vision):
    def next_account_action(
        self,
        png: bytes,
        *,
        goal: str,
        allowed_typing: list[str],
    ) -> dict[str, Any]:
        image, ow, oh = self._resize(png)

        allowed = [
            "tap",
            "swipe",
            "back",
            "wait",
            "none",
            *allowed_typing,
        ]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["act", "done", "blocked", "wait"],
                },
                "action": {
                    "type": "string",
                    "enum": allowed,
                },
                "x": {"type": "number", "minimum": 0, "maximum": 1},
                "y": {"type": "number", "minimum": 0, "maximum": 1},
                "x2": {"type": "number", "minimum": 0, "maximum": 1},
                "y2": {"type": "number", "minimum": 0, "maximum": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "screen": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": [
                "status",
                "action",
                "x",
                "y",
                "x2",
                "y2",
                "confidence",
                "screen",
                "reason",
            ],
        }

        typing_rules = ", ".join(allowed_typing) if allowed_typing else "none"

        prompt = f"""
You are controlling the ACCOUNT UI for Rise of Kingdoms on the user's local
LDPlayer emulator. The user explicitly clicked a localhost web-dashboard button
for this one account action.

GOAL:
{goal}

Look at the screenshot and choose exactly ONE next emulator action.

Coordinates are normalized 0..1 across the full screenshot.

If typing is required, return the matching semantic typing action and put x/y
on the exact text field. IMPORTANT: when an allowed typing action corresponds
to a visible empty field, return that typing action directly instead of merely
returning "tap". The local controller will focus the field and type the value.
You do NOT receive or need password contents.

Allowed typing actions:
{typing_rules}

RULES:
- Follow only the explicit goal above.
- You MAY open the saved-account dropdown, choose the specifically named saved
  account, press Login, choose "Log in to another account", and navigate normal
  account/password screens when the goal asks for it.
- Never select an account different from the explicit target.
- Never expose, infer, repeat, or request password contents.
- If a login-method chooser visibly offers both "Verification Code Login" and
  "Password Login":
  - for password/credential login, choose exactly "Password Login";
  - for email-code login, choose exactly "Verification Code Login".
- A verification code explicitly supplied by the user for the expected email
  verification-code form is allowed. Never attempt to obtain, intercept, read,
  guess, or bypass a code yourself.
- Never purchase anything, spend gems, attack, delete troops, or send chat.
- Unexpected CAPTCHA, 2FA, recovery/security challenges, or identity
  verification => status=blocked. Never bypass them.
- If a requested account label is not visibly present, return blocked rather
  than selecting a similar label.
- status=done only when the requested state is visibly achieved.
""".strip()

        payload = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(image).decode("ascii")],
                }
            ],
            "stream": False,
            "format": schema,
            "think": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 500},
        }

        req = urllib.request.Request(
            OLLAMA + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=160) as response:
            obj = json.loads(response.read().decode("utf-8"))

        msg = obj.get("message") or {}
        for field in ("content", "thinking"):
            raw = str(msg.get(field) or "").strip()
            if not raw:
                continue
            try:
                ans = self._extract(raw)
                ans["_ow"] = ow
                ans["_oh"] = oh
                return ans
            except Exception:
                continue

        raise RuntimeError("Account AI returned no usable action")


class AccountController:
    def __init__(self, device: str):
        emit_progress(
            "connect",
            f"Connecting web account controls to {device}",
            method="adb",
        )
        self.adb = ADB(device)
        self.ai: AccountVision | None = None

    def _ai(self) -> AccountVision:
        if self.ai is None:
            emit_progress(
                "ai_start",
                f"Starting vision fallback model {MODEL}",
                method="ai",
            )
            self.ai = AccountVision()
        return self.ai

    def _tap_norm(self, png: bytes, x: float, y: float) -> None:
        with Image.open(io.BytesIO(png)) as im:
            px = int(max(0.0, min(1.0, float(x))) * im.width)
            py = int(max(0.0, min(1.0, float(y))) * im.height)
        self.adb.tap(px, py)

    def _swipe_norm(
        self,
        png: bytes,
        x: float,
        y: float,
        x2: float,
        y2: float,
    ) -> None:
        with Image.open(io.BytesIO(png)) as im:
            p1x = int(max(0.0, min(1.0, float(x))) * im.width)
            p1y = int(max(0.0, min(1.0, float(y))) * im.height)
            p2x = int(max(0.0, min(1.0, float(x2))) * im.width)
            p2y = int(max(0.0, min(1.0, float(y2))) * im.height)
        self.adb.swipe(p1x, p1y, p2x, p2y, 550)

    def direct_action(self, action: str, req: dict[str, Any]) -> bool:
        """
        Fast path for controls Android exposes through UIAutomator.

        Returning False is not failure; it means use vision fallback.
        """
        emit_progress(
            "direct_probe",
            "Checking Android UI for the requested control",
            method="uiautomator",
        )

        if action == "login_selected":
            return direct_tap_login(self.adb)

        if action == "login_another":
            return direct_tap_text(
                self.adb,
                "Log in to another account",
                exact=True,
            )

        if action == "login_email_code":
            email = str(req.get("email") or "").strip()
            code = str(req.get("verification_code") or "").strip()

            # Usually we're already on the code-entry screen. Type the code and
            # press the submit button immediately.
            if code and direct_type_verification_code(self.adb, code):
                if direct_submit_verification_code(self.adb):
                    emit_progress(
                        "fast_submit",
                        "Verification code entered and submitted directly",
                        method="uiautomator+adb",
                    )
                    return True

            # Recovery path if the expected screen is not open yet.
            switched = direct_tap_verification_code_login(self.adb)
            if switched:
                time.sleep(0.20)

            if email:
                direct_type_email(self.adb, email)

            if code and direct_type_verification_code(self.adb, code):
                if direct_submit_verification_code(self.adb):
                    emit_progress(
                        "fast_submit",
                        "Verification code entered and submitted directly",
                        method="uiautomator+adb",
                    )
                    return True

            return switched

        if action == "prepare_email_code_login":
            email = str(req.get("email") or "").strip()

            switched = direct_tap_verification_code_login(self.adb)
            if switched:
                time.sleep(0.20)

            if email and direct_type_email(self.adb, email):
                return True

            if switched:
                return True

        if action == "login_credentials":
            # RoK often lands on the verification-code login screen first.
            # Switch to the normal password form before typing credentials.
            if direct_tap_password_login(self.adb):
                return True

        if action == "choose_saved":
            account = str(req.get("account") or "").strip()
            if account:
                return direct_tap_text(
                    self.adb,
                    account,
                    exact=True,
                )

        # The dropdown arrow normally has no accessibility label, and credential
        # forms vary across RoK versions. Let the vision fallback handle them.
        return False

    def run_ai_goal(
        self,
        *,
        goal: str,
        values: dict[str, str],
        allowed_typing: list[str],
        max_steps: int = 28,
    ) -> tuple[bool, str, bool]:
        for step in range(1, max_steps + 1):
            png = self.adb.screenshot()

            emit_progress(
                "ai_look",
                f"AI navigation step {step}/{max_steps}",
                method="ai",
            )

            ans = self._ai().next_account_action(
                png,
                goal=goal,
                allowed_typing=allowed_typing,
            )

            status = str(ans.get("status") or "blocked")
            action = str(ans.get("action") or "none")
            confidence = float(ans.get("confidence") or 0)
            reason = str(ans.get("reason") or "")[:300]
            screen = str(ans.get("screen") or "")[:120]

            emit_progress(
                status,
                reason or f"AI chose {action}",
                method="ai",
                screen=screen,
            )

            if status == "done":
                return True, reason or "Account action completed", False

            if status == "blocked":
                return False, reason or "Account action is blocked", True

            if status == "wait" or action == "wait":
                time.sleep(0.45)
                continue

            if confidence < 0.48:
                time.sleep(0.30)
                continue

            if action == "tap":
                self._tap_norm(
                    png,
                    ans.get("x", 0.5),
                    ans.get("y", 0.5),
                )

            elif action == "swipe":
                self._swipe_norm(
                    png,
                    ans.get("x", 0.5),
                    ans.get("y", 0.7),
                    ans.get("x2", 0.5),
                    ans.get("y2", 0.3),
                )

            elif action == "back":
                self.adb.back()

            elif action in allowed_typing:
                value = values.get(action, "")
                if not value:
                    return False, f"Missing value required for {action}", True

                self._tap_norm(
                    png,
                    ans.get("x", 0.5),
                    ans.get("y", 0.5),
                )
                emit_progress(
                    "typing",
                    "Typing private value into the selected Android field",
                    method="adb-private",
                    screen=screen,
                )
                time.sleep(0.25)
                if action in {
                    "type_email_code_address",
                    "type_username",
                }:
                    emit_progress(
                        "typing_email",
                        "Typing the web-supplied email/username into LDPlayer",
                        method="adb-text",
                        screen=screen,
                    )
                    public_type(
                        self.adb,
                        value,
                        clear_first=True,
                    )
                else:
                    private_type(
                        self.adb,
                        value,
                        clear_first=True,
                    )

                    if action == "type_verification_code":
                        if direct_submit_verification_code(self.adb):
                            emit_progress(
                                "fast_submit",
                                "Verification code submitted immediately after typing",
                                method="adb+uiautomator",
                                screen=screen,
                            )
                            return (
                                True,
                                "Verification code entered and Verification Code Login pressed",
                                False,
                            )

            time.sleep(0.25)

        return False, "Account action step limit reached", True

    def execute(
        self,
        req: dict[str, Any],
    ) -> tuple[bool, str, bool]:
        action = str(req.get("action") or "").strip()
        goal, values, allowed_typing = request_definition(req)

        # Use a deterministic emulator tap first when Android exposes the exact
        # control. Then let AI verify/navigate the remaining state.
        try:
            direct_used = self.direct_action(action, req)
        except Exception as exc:
            emit_progress(
                "direct_fallback",
                f"Direct Android UI path unavailable: {exc}",
                method="uiautomator",
            )
            direct_used = False

        if direct_used:
            emit_progress(
                "direct_done",
                "Direct emulator control executed",
                method="uiautomator",
            )

            if action == "login_email_code":
                # The direct path presses Verification Code Login before returning.
                # Avoid a slow AI verification cycle unless the same code form is
                # still visibly present.
                time.sleep(0.55)
                code_field = find_editable_field(
                    self.adb,
                    keywords=("verification code", "email code", "code"),
                )
                submit_button = find_ui_text(
                    self.adb,
                    "Verification Code Login",
                    exact=True,
                )
                if not (code_field and submit_button):
                    emit_progress(
                        "done",
                        "Verification Code Login pressed",
                        method="uiautomator+adb",
                    )
                    return (
                        True,
                        "Verification code entered and Verification Code Login pressed",
                        False,
                    )

            time.sleep(0.15)

        return self.run_ai_goal(
            goal=goal,
            values=values,
            allowed_typing=allowed_typing,
            max_steps=12,
        )


def request_definition(
    req: dict[str, Any],
) -> tuple[str, dict[str, str], list[str]]:
    action = str(req.get("action") or "").strip()

    if action == "open_dropdown":
        return (
            "On the centered Rise of Kingdoms Login popup, open the saved-account "
            "dropdown by tapping the account row/dropdown arrow. Return done once "
            "the saved-account list is visibly open.",
            {},
            [],
        )

    if action == "choose_saved":
        account = str(req.get("account") or "").strip()
        if not account:
            raise RuntimeError("Saved account label/email is required")
        return (
            "Select exactly the saved account whose visible label/email is "
            f"{account!r}. Open the saved-account dropdown if needed. Do not "
            "select any other account. Return done when that exact account is "
            "visibly selected on the Login popup.",
            {},
            [],
        )

    if action == "login_selected":
        return (
            "On the normal Rise of Kingdoms Login popup, press the large Login "
            "button for the account that is already selected. Wait through normal "
            "loading. Return done when the Login popup is gone and the game is "
            "loading or normal gameplay UI is visible. Do not change accounts.",
            {},
            [],
        )

    if action == "login_another":
        return (
            "From the Rise of Kingdoms Login popup, choose exactly "
            "'Log in to another account'. Return done when the credential/new "
            "account login form is visibly ready.",
            {},
            [],
        )

    if action == "prepare_email_code_login":
        email = str(req.get("email") or "").strip()
        if not email:
            raise RuntimeError("Email address is required")

        return (
            "Prepare email verification-code login. If the saved-account Login "
            "popup is visible, choose 'Log in to another account'. If the login "
            "method chooser offers 'Verification Code Login' and 'Password Login', "
            "choose exactly 'Verification Code Login'. On the verification-code "
            "form, the Email Address field MUST receive the supplied email. "
            "When that field is visible, return type_email_code_address directly "
            "instead of returning a plain tap. After the email has been entered, "
            "if the form has a normal button or "
            "control to send/request/get the verification code, press it. Return "
            "done once the email is entered and the screen is waiting for the "
            "verification code that the user will provide. Do not attempt to "
            "obtain, read, intercept, guess, or bypass the code.",
            {"type_email_code_address": email},
            ["type_email_code_address"],
        )

    if action == "login_email_code":
        email = str(req.get("email") or "").strip()
        code = str(req.get("verification_code") or "").strip()
        if not email:
            raise RuntimeError("Email address is required")
        if not code:
            raise RuntimeError("Verification code is required")

        return (
            "Complete the expected email verification-code login using the code "
            "explicitly supplied by the user. If needed, choose 'Log in to another "
            "account', then choose exactly 'Verification Code Login'. Make sure the "
            "Email Address field contains the supplied email by requesting "
            "type_email_code_address if needed. Request type_verification_code on "
            "the verification-code field, then immediately press the large button "
            "labeled exactly 'Verification Code Login'. Return done when the "
            "verification form is gone and "
            "the game is loading or normal gameplay is visible. Do not request a "
            "different code or attempt to obtain one yourself. Block on CAPTCHA, "
            "2FA, recovery challenge, or any different identity-verification flow.",
            {
                "type_email_code_address": email,
                "type_verification_code": code,
            },
            ["type_email_code_address", "type_verification_code"],
        )

    if action == "login_credentials":
        username = str(req.get("username") or "").strip()
        password = str(req.get("password") or "")
        if not username or not password:
            raise RuntimeError("Email/username and password are required")

        return (
            "Log in to another account. If the saved-account Login popup is "
            "visible, choose 'Log in to another account'. IMPORTANT: if the next "
            "screen shows a large 'Verification Code Login' button and a smaller "
            "'Password Login' control underneath, press exactly 'Password Login' "
            "BEFORE entering credentials. Once the password-login form is visible, "
            "request type_username for the Email Address/username field and "
            "type_password for the password field. Then press the normal "
            "Login/Continue button. Return done when the credential form is gone "
            "and the game is loading or normal gameplay is visible. Block on "
            "CAPTCHA, OTP, 2FA, recovery challenge, or other identity verification.",
            {
                "type_username": username,
                "type_password": password,
            },
            ["type_username", "type_password"],
        )

    if action == "change_password":
        current_password = str(req.get("current_password") or "")
        new_password = str(req.get("new_password") or "")
        if not current_password or not new_password:
            raise RuntimeError("Current and new passwords are required")
        if current_password == new_password:
            raise RuntimeError(
                "New password must be different from current password"
            )

        return (
            "Change the password for the CURRENTLY ACTIVE account only. Navigate "
            "the normal account/settings UI to the legitimate Change Password "
            "screen. Request type_current_password on the current-password field, "
            "type_new_password on the new-password field, and "
            "type_confirm_password on the confirmation field. Submit the normal "
            "Change/Save Password button. Return done only when the UI visibly "
            "confirms the password was changed. Block on CAPTCHA, OTP, 2FA, "
            "recovery challenge, or identity verification.",
            {
                "type_current_password": current_password,
                "type_new_password": new_password,
                "type_confirm_password": new_password,
            },
            [
                "type_current_password",
                "type_new_password",
                "type_confirm_password",
            ],
        )

    raise RuntimeError("Unsupported account action")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RoK localhost web account-control worker"
    )
    parser.add_argument("--device", required=True)
    args = parser.parse_args()

    raw = sys.stdin.read()
    req: dict[str, Any] = {}
    values: dict[str, str] = {}

    try:
        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            raise RuntimeError("Invalid request")
        req = parsed

        controller = AccountController(args.device)
        ok, message, blocked = controller.execute(req)
        emit_result(ok, message, blocked=blocked)
        return 0 if ok else 3

    except Exception as exc:
        emit_result(False, str(exc), blocked=True)
        return 1

    finally:
        raw = ""
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
        values.clear()


if __name__ == "__main__":
    raise SystemExit(main())
