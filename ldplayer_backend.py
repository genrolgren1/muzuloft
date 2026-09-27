from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

VERSION = "3.5.1"

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_APK_DIR = PROJECT_ROOT / "apk"
KS_HOME = Path(
    os.environ.get("KS_HOME")
    or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KingdomServices")
)
KS_HOME.mkdir(parents=True, exist_ok=True)

ROK_PACKAGE = os.environ.get(
    "KS_ROK_PACKAGE",
    "com.lilithgame.roc.gp",
).strip() or "com.lilithgame.roc.gp"

FRIDA_TEST_PACKAGE = "com.ks.fridatest"

_SERIAL_CACHE: dict[int, str] = {}


def hidden_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run(cmd: list[str], *, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        [str(x) for x in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        creationflags=hidden_flags(),
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            (cp.stdout or "").strip()
            or f"Command failed: {' '.join(str(x) for x in cmd)}"
        )
    return cp


def _candidate_install_dirs() -> list[Path]:
    values: list[Path] = []

    for env_name in ("LDPLAYER_HOME", "LDPLAYER_PATH"):
        raw = os.environ.get(env_name, "").strip().strip('"')
        if raw:
            p = Path(raw)
            values.append(p.parent if p.is_file() else p)

    for exe in ("ldconsole.exe", "dnconsole.exe"):
        found = shutil.which(exe)
        if found:
            values.append(Path(found).resolve().parent)

    for drive in ("C:", "D:", "E:", "F:"):
        for rel in (
            r"\LDPlayer\LDPlayer14",
            r"\LDPlayer\LDPlayer9",
            r"\leidian\LDPlayer14",
            r"\leidian\LDPlayer9",
            r"\Program Files\LDPlayer\LDPlayer14",
            r"\Program Files\LDPlayer\LDPlayer9",
            r"\Program Files (x86)\LDPlayer\LDPlayer14",
            r"\Program Files (x86)\LDPlayer\LDPlayer9",
        ):
            values.append(Path(drive + rel))

    seen: set[str] = set()
    result: list[Path] = []
    for p in values:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def ldplayer_home() -> Path:
    for home in _candidate_install_dirs():
        if (home / "ldconsole.exe").exists() or (home / "dnconsole.exe").exists():
            return home
    raise RuntimeError(
        "LDPlayer 9/14 was not found. If needed, set LDPLAYER_HOME to "
        "the folder containing ldconsole.exe."
    )


def find_ldconsole() -> str:
    home = ldplayer_home()
    for name in ("ldconsole.exe", "dnconsole.exe"):
        p = home / name
        if p.exists():
            return str(p)
    raise RuntimeError("LDPlayer console executable was not found")


def find_adb() -> str:
    """
    Prefer LDPlayer's bundled adb.exe so we use the same ADB server that
    LDPlayer uses to register emulator-* devices.
    """
    override = os.environ.get("ADB_PATH", "").strip().strip('"')
    if override and Path(override).exists():
        return override

    home = ldplayer_home()
    for p in (
        home / "adb.exe",
        home / "adb" / "adb.exe",
        home / "platform-tools" / "adb.exe",
    ):
        if p.exists():
            return str(p)

    fallback = KS_HOME / "android-sdk" / "platform-tools" / "adb.exe"
    if fallback.exists():
        return str(fallback)

    found = shutil.which("adb")
    if found:
        return found

    raise RuntimeError("adb.exe could not be found")


def ldconsole(*args: str, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess:
    return run(
        [find_ldconsole(), *[str(x) for x in args]],
        timeout=timeout,
        check=check,
    )


def list_instances() -> list[dict[str, Any]]:
    cp = ldconsole("list2", timeout=20)
    if cp.returncode != 0:
        raise RuntimeError((cp.stdout or "").strip() or "ldconsole list2 failed")

    result: list[dict[str, Any]] = []
    for raw in (cp.stdout or "").splitlines():
        parts = [x.strip() for x in raw.strip().split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue

        def _int_at(pos: int) -> int:
            try:
                return int(parts[pos], 0)
            except Exception:
                return 0

        result.append(
            {
                "index": index,
                "name": parts[1],
                "top_hwnd": _int_at(2),
                "bind_hwnd": _int_at(3),
                "android_started": len(parts) > 4 and parts[4] == "1",
                "pid": parts[5] if len(parts) > 5 else "",
                "raw": raw.strip(),
            }
        )
    return result



def is_frida_test_instance(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    return str(item.get("name") or "").strip().upper() == "KS_FRIDA_TEST"


def select_main_index() -> int:
    """
    Reuse an existing normal LDPlayer instead of creating a second one.

    Preference:
    1. already-running non-Frida instance
    2. existing non-Frida instance
    3. index 0 only if there is no normal instance at all
    """
    override = os.environ.get("KS_LD_INDEX", "").strip()
    if override and override.upper() != "AUTO":
        try:
            return max(0, int(override))
        except Exception:
            pass

    items = [x for x in list_instances() if not is_frida_test_instance(x)]
    running = [x for x in items if x.get("android_started")]

    if running:
        return int(sorted(running, key=lambda x: x["index"])[0]["index"])
    if items:
        return int(sorted(items, key=lambda x: x["index"])[0]["index"])
    return 0


def stop_leftover_frida_test(except_index: int | None = None) -> None:
    for item in list_instances():
        if not is_frida_test_instance(item):
            continue
        idx = int(item["index"])
        if except_index is not None and idx == int(except_index):
            continue
        if item.get("android_started"):
            ldconsole("quit", "--index", str(idx), timeout=30, check=False)


def _show_window_handle(hwnd: int, command: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.ShowWindow(int(hwnd), int(command)))
    except Exception:
        return False


def hide_instance_window(index: int) -> bool:
    """
    Hide only this LDPlayer's GUI. Android keeps running in the background.

    list2 exposes top-level and bind window handles, so we do not hide unrelated
    LDPlayer windows or the Windows desktop.
    """
    item = instance(index)
    if not item:
        return False

    # SW_HIDE = 0
    changed = False
    for hwnd in (item.get("top_hwnd", 0), item.get("bind_hwnd", 0)):
        if hwnd:
            _show_window_handle(int(hwnd), 0)
            changed = True
    return changed


def show_instance_window(index: int) -> bool:
    item = instance(index)
    if not item:
        return False

    # SW_SHOW = 5
    changed = False
    for hwnd in (item.get("top_hwnd", 0), item.get("bind_hwnd", 0)):
        if hwnd:
            _show_window_handle(int(hwnd), 5)
            changed = True
    return changed


def keep_instance_headless(index: int, *, seconds: float = 8.0) -> None:
    deadline = time.monotonic() + max(0.5, float(seconds))
    while time.monotonic() < deadline:
        hide_instance_window(index)
        time.sleep(0.25)



def instance(index: int) -> dict[str, Any] | None:
    index = max(0, int(index))
    return next((x for x in list_instances() if x["index"] == index), None)


def ensure_instance_exists(index: int, *, name: str | None = None) -> dict[str, Any]:
    index = max(0, int(index))
    while True:
        existing = instance(index)
        if existing:
            return existing

        items = list_instances()
        next_index = max([x["index"] for x in items], default=-1) + 1
        create_name = (
            name
            if next_index == index and name
            else ("RoK Gem Bot" if next_index == 0 else f"RoK Gem Bot {next_index}")
        )
        cp = ldconsole("add", "--name", create_name, timeout=90)
        if cp.returncode != 0:
            raise RuntimeError(
                (cp.stdout or "").strip()
                or f"Could not create LDPlayer index {next_index}"
            )
        time.sleep(1)


def is_running(index: int) -> bool:
    item = instance(index)
    return bool(item and item.get("android_started"))


def _adb_config_path(index: int) -> Path:
    if int(index) < 0:
        raise ValueError('LDPlayer index must be non-negative')
    directory = (ldplayer_home() / 'vms' / 'config').resolve()
    path = (directory / f'leidian{int(index)}.config').resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise RuntimeError(f'Cannot safely locate LDPlayer ADB settings for index {index}')
    return path


def _read_adb_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise RuntimeError('LDPlayer instance configuration is not an object')
    value = data.get('basicSettings.adbDebug', 0)
    if type(value) is not int or value not in (0, 1, 2):
        raise RuntimeError('Unrecognized LDPlayer ADB setting; configuration left unchanged')
    return data


def _write_local_adb_config(path: Path) -> bool:
    data = _read_adb_config(path)
    if data.get('basicSettings.adbDebug') == 1:
        return False
    # Preserve the exact original, including unrelated emulator settings.
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns())
    backup = path.with_name(path.name + '.gemops-adb-' + stamp + '.bak')
    shutil.copy2(path, backup)
    data['basicSettings.adbDebug'] = 1  # LDPlayer: Open local connection.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name+'.gemops-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=4)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if _read_adb_config(path).get('basicSettings.adbDebug') != 1:
            raise RuntimeError('LDPlayer did not retain the local ADB setting')
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return True


def ensure_local_adb_enabled(index: int) -> bool:
    path = _adb_config_path(index)
    if _read_adb_config(path).get('basicSettings.adbDebug') == 1:
        return False
    def active():
        item = instance(index) or {}
        return bool(item.get('android_started') or int(item.get('pid') or 0) > 0)
    if active():
        print(f'[LDPLAYER] Enabling local ADB; restarting selected instance {index}', flush=True)
        if not stop_instance(index):
            raise RuntimeError('Could not stop selected LDPlayer to enable ADB')
        deadline = time.monotonic() + 30
        while active():
            if time.monotonic() >= deadline:
                raise RuntimeError('LDPlayer has not stopped; ADB settings left unchanged')
            time.sleep(.25)
    # Re-read after shutdown: LDPlayer may save its current settings on exit.
    changed = _write_local_adb_config(path)
    _SERIAL_CACHE.pop(index, None)
    if changed:
        print(f'[LDPLAYER] Local ADB enabled for instance {index}', flush=True)
    return changed


def start_instance(index: int) -> None:
    index = max(0, int(index))
    ensure_instance_exists(index)
    ensure_local_adb_enabled(index)

    if not is_running(index):
        cp = ldconsole("launch", "--index", str(index), timeout=45)
        if cp.returncode != 0:
            raise RuntimeError(
                (cp.stdout or "").strip()
                or f"Could not launch LDPlayer index {index}"
            )


def stop_instance(index: int) -> bool:
    index = max(0, int(index))
    if instance(index) is None:
        return False

    cp = ldconsole("quit", "--index", str(index), timeout=30)
    _SERIAL_CACHE.pop(index, None)
    return cp.returncode == 0


def restart_instance(index: int, *, root: bool | None = None) -> None:
    stop_instance(index)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and is_running(index):
        time.sleep(0.4)
    start_instance(index)


def configure_instance(
    index: int,
    *,
    root: bool | None = None,
    cpu: int | None = None,
    memory: int | None = None,
    resolution: str | None = None,
) -> None:
    args = ["modify", "--index", str(max(0, int(index)))]

    if resolution:
        args += ["--resolution", str(resolution)]
    if cpu is not None:
        args += ["--cpu", str(int(cpu))]
    if memory is not None:
        args += ["--memory", str(int(memory))]
    if root is not None:
        args += ["--root", "1" if bool(root) else "0"]

    if len(args) > 3:
        cp = ldconsole(*args, timeout=60)
        if cp.returncode != 0:
            raise RuntimeError(
                (cp.stdout or "").strip()
                or f"Could not configure LDPlayer index {index}"
            )


def _wait_android_started(index: int, timeout: int = 120) -> None:
    deadline = time.monotonic() + max(20, int(timeout))
    while time.monotonic() < deadline:
        item = instance(index)
        if item and item.get("android_started"):
            return
        time.sleep(1)

    raise RuntimeError(
        f"LDPlayer index {index} opened, but LDPlayer did not report Android started."
    )


def _restart_adb_server() -> None:
    adb = find_adb()
    run([adb, "kill-server"], timeout=10)
    time.sleep(0.5)
    run([adb, "start-server"], timeout=10)


def _adb_devices() -> list[str]:
    adb = find_adb()
    cp = run([adb, "devices"], timeout=10)
    devices: list[str] = []

    for raw in (cp.stdout or "").splitlines()[1:]:
        parts = raw.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def expected_serial(index: int) -> str:
    return f"emulator-{5554 + max(0, int(index)) * 2}"


def serial_candidates(index: int) -> list[str]:
    i = max(0, int(index))
    return [
        f"emulator-{5554 + i * 2}",
        f"127.0.0.1:{5555 + i * 2}",
        f"emulator-{5555 + i * 2}",
    ]



def resolve_adb_serial(index: int, *, timeout: int = 60) -> str:
    index = max(0, int(index))

    cached = _SERIAL_CACHE.get(index)
    if cached:
        adb = find_adb()
        cp = run([adb, "-s", cached, "get-state"], timeout=5)
        if cp.returncode == 0 and (cp.stdout or "").strip() == "device":
            return cached
        _SERIAL_CACHE.pop(index, None)

    _wait_android_started(index, timeout=timeout)

    # Never restart the shared ADB server here: that destroys companion forwards
    # and interrupts unrelated emulators. Starting an existing server is harmless.
    run([find_adb(), "start-server"], timeout=10)

    wanted = expected_serial(index)
    deadline = time.monotonic() + max(10, int(timeout))
    last_devices: list[str] = []

    while time.monotonic() < deadline:
        devices = _adb_devices()
        last_devices = devices

        for candidate in serial_candidates(index):
            if candidate in devices:
                _SERIAL_CACHE[index] = candidate
                return candidate

        # Do not assign an unrelated Android emulator merely because it is the
        # only visible device. The selected LDPlayer must have a matching serial.

        time.sleep(1)

    raise RuntimeError(
        "LDPLAYER_ADB_SETUP_REQUIRED\n"
        f"LDPlayer index {index} is running but is not visible in `adb devices`.\n"
        f"Expected something like: {wanted}\n"
        f"Visible ADB devices: {last_devices or 'none'}\n\n"
        "The launcher enables local ADB automatically. The connection is still unavailable.\n"
        "Run LDPLAYER_DIAGNOSTICS.bat to inspect the selected instance's boot and transport state."

    )


def serial_for_index(index: int = 0) -> str:
    return f"LDPLAYER:{max(0, int(index))}"


def parse_ldplayer(value: str) -> int | None:
    match = re.fullmatch(
        r"(?:LDPLAYER|LD)\s*:\s*(\d+)",
        (value or "").strip(),
        flags=re.I,
    )
    return int(match.group(1)) if match else None


def adb_connect(index: int, *, timeout: int = 90) -> str:
    serial = resolve_adb_serial(index, timeout=timeout)
    print(f"[LDPLAYER] ADB mapped index {index} -> {serial}", flush=True)
    return serial


def ld_adb(
    index: int,
    *args: str,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess:
    adb = find_adb()
    serial = resolve_adb_serial(index, timeout=min(max(timeout, 20), 90))

    cp = run(
        [adb, "-s", serial, *[str(x) for x in args]],
        timeout=timeout,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            (cp.stdout or "").strip()
            or f"ADB command failed on LDPlayer index {index}"
        )
    return cp


def ld_adb_ready(index: int) -> tuple[bool, str]:
    try:
        serial = resolve_adb_serial(index, timeout=20)
        cp = ld_adb(
            index,
            "shell",
            "getprop",
            "sys.boot_completed",
            timeout=10,
            check=False,
        )
        text = (cp.stdout or "").strip()
        return cp.returncode == 0 and text.endswith("1"), f"{serial}: {text}"
    except Exception as exc:
        return False, str(exc)


def ld_screenshot_bytes(index: int) -> bytes:
    adb = find_adb()
    serial = resolve_adb_serial(index)

    cp = subprocess.run(
        [adb, "-s", serial, "exec-out", "screencap", "-p"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=25,
        creationflags=hidden_flags(),
    )
    if cp.returncode != 0:
        raise RuntimeError(
            (cp.stderr or b"").decode(errors="ignore").strip()
            or "LDPlayer screenshot failed"
        )

    data = cp.stdout or b""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("LDPlayer screenshot was not a valid PNG")
    return data


def ld_tap(index: int, x: int, y: int) -> None:
    ld_adb(index, "shell", "input", "tap", str(int(x)), str(int(y)), timeout=12)


def ld_swipe(
    index: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    ms: int = 500,
) -> None:
    ld_adb(
        index,
        "shell",
        "input",
        "swipe",
        str(int(x1)),
        str(int(y1)),
        str(int(x2)),
        str(int(y2)),
        str(int(ms)),
        timeout=15,
    )


def ld_back(index: int) -> None:
    ld_adb(index, "shell", "input", "keyevent", "4", timeout=10)


def ld_package_installed(index: int, package: str) -> bool:
    cp = ld_adb(
        index,
        "shell",
        "pm",
        "path",
        package,
        timeout=15,
        check=False,
    )
    return cp.returncode == 0 and "package:" in (cp.stdout or "")


def _find_local_installer() -> Path | None:
    if not PROJECT_APK_DIR.exists():
        return None

    files = [p for p in PROJECT_APK_DIR.iterdir() if p.is_file()]
    apks = [p for p in files if p.suffix.lower() == ".apk"]
    if apks:
        return sorted(apks)[0]

    bundles = [
        p for p in files
        if p.suffix.lower() in {".xapk", ".apks", ".zip"}
    ]
    return sorted(bundles)[0] if bundles else None


def _install_bundle(index: int, installer: Path) -> None:
    adb = find_adb()
    serial = resolve_adb_serial(index)

    if installer.suffix.lower() == ".apk":
        cp = run(
            [adb, "-s", serial, "install", "-r", str(installer)],
            timeout=300,
        )
        if cp.returncode != 0 or "success" not in (cp.stdout or "").lower():
            raise RuntimeError(
                (cp.stdout or "").strip()
                or f"Could not install {installer.name}"
            )
        return

    with tempfile.TemporaryDirectory(prefix="rok-bundle-") as td:
        root = Path(td)
        with zipfile.ZipFile(installer, "r") as archive:
            archive.extractall(root)

        apks = sorted(root.rglob("*.apk"))
        if not apks:
            raise RuntimeError(f"{installer.name} did not contain APK files")

        cp = run(
            [
                adb,
                "-s",
                serial,
                "install-multiple",
                "-r",
                *[str(p) for p in apks],
            ],
            timeout=420,
        )
        if cp.returncode != 0 or "success" not in (cp.stdout or "").lower():
            raise RuntimeError(
                (cp.stdout or "").strip()
                or "Split APK installation failed"
            )


def ld_foreground_package(index: int) -> str:
    """
    Return the package currently resumed on this LDPlayer instance.
    Best-effort only; an empty string means it could not be determined.
    """
    cp = ld_adb(
        index,
        "shell",
        "dumpsys",
        "activity",
        "activities",
        timeout=15,
        check=False,
    )
    text = cp.stdout or ""

    patterns = (
        r"(?:mResumedActivity|topResumedActivity|ResumedActivity)[^\n]*?\s([A-Za-z0-9._]+)/",
        r"(?:mFocusedApp|mCurrentFocus)[^\n]*?\s([A-Za-z0-9._]+)/",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()

    return ""


def stop_frida_test_app_on_index(index: int) -> bool:
    """
    Force-stop only the isolated local Frida test app on this instance.

    This does not touch Rise of Kingdoms data or any other package.
    """
    try:
        cp = ld_adb(
            index,
            "shell",
            "am",
            "force-stop",
            FRIDA_TEST_PACKAGE,
            timeout=12,
            check=False,
        )
        return cp.returncode == 0
    except Exception:
        return False


def ensure_normal_rok_foreground(
    index: int,
    *,
    wait_seconds: float = 12.0,
    only_if_test_app: bool = False,
) -> bool:
    """
    Normal GemOps runtime isolation.

    If the local Frida Test Lab app is foreground, close it and launch RoK.
    When only_if_test_app=False, RoK is launched whenever it is not foreground.
    """
    index = max(0, int(index))
    current = ld_foreground_package(index)

    if current.lower() == FRIDA_TEST_PACKAGE.lower():
        stop_frida_test_app_on_index(index)
        time.sleep(0.15)
    elif only_if_test_app:
        return False

    ok = ld_ensure_rok_running(
        index,
        wait_seconds=wait_seconds,
    )

    if not ok:
        raise RuntimeError(
            f"RoK did not become foreground on LDPlayer index {index}"
        )

    return True


def ld_rok_is_foreground(index: int) -> bool:
    cp = ld_adb(
        index,
        "shell",
        "dumpsys",
        "activity",
        "activities",
        timeout=15,
        check=False,
    )
    text = (cp.stdout or "").lower()
    return ROK_PACKAGE.lower() in text and (
        "mresumedactivity" in text
        or "resumedactivity" in text
        or "topresumedactivity" in text
    )


def ld_ensure_rok_running(index: int, *, wait_seconds: float = 10.0) -> bool:
    if not ld_package_installed(index, ROK_PACKAGE):
        installer = _find_local_installer()
        if not installer:
            raise RuntimeError(
                f"{ROK_PACKAGE} is not installed in LDPlayer index {index}. "
                f"Install RoK there or place its APK/XAPK in {PROJECT_APK_DIR}."
            )
        _install_bundle(index, installer)

    if ld_rok_is_foreground(index):
        return True

    cp = ldconsole(
        "runapp",
        "--index",
        str(max(0, int(index))),
        "--packagename",
        ROK_PACKAGE,
        timeout=30,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            (cp.stdout or "").strip() or f"Could not launch {ROK_PACKAGE}"
        )

    deadline = time.monotonic() + max(2, float(wait_seconds))
    while time.monotonic() < deadline:
        if ld_rok_is_foreground(index):
            return True
        time.sleep(0.5)
    return False


# Compatibility helpers for explicit non-LDPlayer serials.
def package_installed(adb: str, serial: str, package: str) -> bool:
    cp = run(
        [adb, "-s", serial, "shell", "pm", "path", package],
        timeout=12,
    )
    return cp.returncode == 0 and "package:" in (cp.stdout or "")


def rok_is_foreground(adb: str, serial: str) -> bool:
    cp = run(
        [adb, "-s", serial, "shell", "dumpsys", "activity", "activities"],
        timeout=12,
    )
    text = (cp.stdout or "").lower()
    return ROK_PACKAGE.lower() in text and "resumedactivity" in text


def ensure_rok_running(
    adb: str,
    serial: str,
    *,
    wait_seconds: float = 8.0,
) -> bool:
    if not package_installed(adb, serial, ROK_PACKAGE):
        return False
    if rok_is_foreground(adb, serial):
        return True

    run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "monkey",
            "-p",
            ROK_PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        timeout=20,
    )

    deadline = time.monotonic() + max(2, float(wait_seconds))
    while time.monotonic() < deadline:
        if rok_is_foreground(adb, serial):
            return True
        time.sleep(0.5)
    return False


def ensure_ldplayer(
    index: int = 0,
    *,
    launch_game: bool = True,
    root: bool | None = None,
    name: str | None = None,
) -> str:
    index = max(0, int(index))
    ensure_instance_exists(index, name=name)

    # The required companion runs in its own instance alongside the main game.
    if launch_game and is_frida_test_instance(instance(index)):
        raise RuntimeError("Refusing to launch RoK in the isolated companion instance")
    if root is not None:
        configure_instance(index, root=root)

    if os.environ.get("KS_LD_TUNE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        configure_instance(
            index,
            cpu=int(os.environ.get("KS_LD_CPU", "2") or 2),
            memory=int(os.environ.get("KS_LD_MEMORY", "2048") or 2048),
            resolution=os.environ.get(
                "KS_LD_RESOLUTION",
                "1280,720,240",
            ).strip(),
        )

    start_instance(index)
    _wait_android_started(index, timeout=120)

    headless = os.environ.get("KS_LD_HEADLESS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }

    if headless:
        keep_instance_headless(index, seconds=2.0)

    try:
        adb_connect(index, timeout=60)
    except Exception:
        # Local ADB is already enabled. Reveal the selected instance for a
        # remaining boot/transport fault instead of choosing another emulator.
        show_instance_window(index)
        raise

    if launch_game:
        ensure_normal_rok_foreground(
            index,
            wait_seconds=12,
            only_if_test_app=False,
        )

    if headless:
        keep_instance_headless(index, seconds=3.0)

    return f"LDPLAYER:{index}"


def ensure_for_scanner(preferred: str = "") -> str:
    preferred = (preferred or "LDPLAYER:0").strip()
    idx = parse_ldplayer(preferred)

    if idx is not None:
        ensure_ldplayer(idx, launch_game=True)
        return f"LDPLAYER:{idx}"

    if preferred.upper() == "AUTO":
        idx = int(os.environ.get("KS_LD_INDEX", "0") or 0)
        ensure_ldplayer(idx, launch_game=True)
        return f"LDPLAYER:{idx}"

    return preferred


def ensure_headless(index: int = 0, *, launch_game: bool = True) -> str:
    return ensure_ldplayer(index, launch_game=launch_game)


def avd_name(index: int) -> str:
    return f"LDPLAYER:{int(index)}"


def stop_avd(name: str) -> bool:
    match = re.search(r"(\d+)$", str(name or ""))
    return stop_instance(int(match.group(1))) if match else False


def probe_rok_renderers(target: str) -> str:
    idx = parse_ldplayer(target)
    if idx is None:
        raise RuntimeError(f"Invalid LDPlayer target: {target}")
    return adb_connect(idx, timeout=30)


def status(index: int = 0) -> dict[str, Any]:
    index = max(0, int(index))
    item = instance(index)

    try:
        _restart_adb_server()
        devices = _adb_devices()
    except Exception:
        devices = []

    result: dict[str, Any] = {
        "backend": "ldplayer",
        "version": VERSION,
        "index": index,
        "name": (item or {}).get("name", ""),
        "running": bool(item and item.get("android_started")),
        "expected_serial": expected_serial(index),
        "adb_devices": devices,
        "ldplayer_home": str(ldplayer_home()),
        "ldconsole": find_ldconsole(),
        "adb": find_adb(),
        "serial": "",
    }

    if result["running"]:
        try:
            result["serial"] = resolve_adb_serial(index, timeout=12)
        except Exception as exc:
            result["error"] = str(exc)

    return result


def main() -> int:
    p = argparse.ArgumentParser(description="RoK Gem Bot LDPlayer backend")
    p.add_argument("--version", action="store_true")
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--start", action="store_true")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--restart", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--select-main", action="store_true")
    p.add_argument("--hide", action="store_true")
    p.add_argument("--show", action="store_true")
    p.add_argument("--stop-frida-test", action="store_true")
    p.add_argument("--launch-rok", action="store_true")
    p.add_argument("--foreground-package", action="store_true")
    p.add_argument("--index", type=int, default=0)
    args = p.parse_args()

    if args.version:
        print(VERSION)
        return 0

    if args.select_main:
        print(select_main_index())
        return 0

    if args.stop_frida_test:
        stop_leftover_frida_test()
        print("stopped")
        return 0

    if args.foreground_package:
        ensure_ldplayer(args.index, launch_game=False)
        print(ld_foreground_package(args.index))
        return 0

    if args.launch_rok:
        ensure_ldplayer(args.index, launch_game=True)
        print(ROK_PACKAGE)
        return 0

    if args.hide:
        hide_instance_window(args.index)
        return 0

    if args.show:
        show_instance_window(args.index)
        return 0

    if args.status:
        print(json.dumps(status(args.index), indent=2))
        return 0

    if args.stop:
        return 0 if stop_instance(args.index) else 1

    if args.restart:
        restart_instance(args.index)
        ensure_ldplayer(args.index, launch_game=False)
        print(resolve_adb_serial(args.index))
        return 0

    if args.prepare or args.start:
        ensure_ldplayer(args.index, launch_game=args.start)
        print(resolve_adb_serial(args.index))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
