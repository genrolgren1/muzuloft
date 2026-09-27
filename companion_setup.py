from __future__ import annotations

import json
import lzma
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from ldplayer_backend import ensure_ldplayer, serial_for_index as ld_serial_for_index, ld_adb, ld_adb_ready

VERSION = "3.5.1"
KS_HOME = Path(
    os.environ.get("KS_HOME")
    or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KingdomServices")
)
SDK_ROOT = KS_HOME / "android-sdk"
JRE_ROOT = KS_HOME / "jre17"
JDK_ROOT = KS_HOME / "jdk17"
LAB_ROOT = KS_HOME / "frida_test_lab"
JDK_DOWNLOAD = LAB_ROOT / "temurin-jdk17.zip"
LDPLAYER_INDEX = int(os.environ.get("FRIDA_LDPLAYER_INDEX", "1") or 1)
LDPLAYER_NAME = "KS_FRIDA_TEST"
SERIAL = ld_serial_for_index(LDPLAYER_INDEX)
TEST_PACKAGE = "com.ks.fridatest"
TEST_ACTIVITY = f"{TEST_PACKAGE}/.FridaTestActivity"
PLATFORM = "platforms;android-35"
BUILD_TOOLS = "build-tools;35.0.0"
LAB_LOG = LAB_ROOT / "frida_lab.log"
APK_PATH = LAB_ROOT / "FridaTest.apk"
FRIDA_REMOTE_LOCAL_PORT = 27052
FRIDA_REMOTE_DEVICE_PORT = 27043
AGENT_ROOT = LAB_ROOT / "agent17"
AGENT_SOURCE = AGENT_ROOT / "agent.js"
AGENT_BUNDLE = AGENT_ROOT / "agent.compiled.js"
PRIVATE_NODE_ROOT = KS_HOME / "node"
_FORWARD_READY = False
_DEVICE = None

LAB_ROOT.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LAB_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def hidden_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], *, timeout: int = 120, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        [str(x) for x in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        creationflags=hidden_flags(),
        env=env,
    )
    if check and cp.returncode != 0:
        raise RuntimeError((cp.stdout or "").strip() or f"Command failed: {cmd}")
    return cp


def sdkmanager() -> str:
    for p in (
        SDK_ROOT / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
        SDK_ROOT / "cmdline-tools" / "latest" / "bin" / "sdkmanager",
    ):
        if p.exists():
            return str(p)
    raise RuntimeError(
        "Android SDK build tools are missing. Keep the existing "
        "%LOCALAPPDATA%\\KingdomServices\\android-sdk folder from the previous build; "
        "LDPlayer replaces only the emulator/runtime."
    )


def _jdk_tool(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return JDK_ROOT / "bin" / f"{name}{suffix}"


def _jre_tool(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return JRE_ROOT / "bin" / f"{name}{suffix}"


def ensure_private_jdk17() -> Path:
    """
    Ensure a private JDK 17 exists for compiling the bundled test APK.

    Kingdom Services previously shipped only a JRE, which is enough to run the
    Android SDK tools but does not contain javac. This JDK is isolated under
    %LOCALAPPDATA%\\KingdomServices\\jdk17 and does not modify system PATH.
    """
    javac = _jdk_tool("javac")
    if javac.exists():
        return JDK_ROOT

    LAB_ROOT.mkdir(parents=True, exist_ok=True)
    log("javac is missing. Installing private Eclipse Temurin JDK 17...")

    api_url = (
        "https://api.adoptium.net/v3/binary/latest/17/ga/"
        "windows/x64/jdk/hotspot/normal/eclipse"
    )

    # Download directly through Python so no external curl/PowerShell dependency
    # is required. The Adoptium API redirects to the current GA binary.
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "RoK-Gem-Bot-Frida-Lab/2.1.1"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        with JDK_DOWNLOAD.open("wb") as dst:
            read = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                read += len(chunk)
                if total and (read == len(chunk) or read % (20 * 1024 * 1024) < len(chunk)):
                    log(f"JDK download: {read // (1024*1024)} / {total // (1024*1024)} MB")

    if not JDK_DOWNLOAD.exists() or JDK_DOWNLOAD.stat().st_size < 20 * 1024 * 1024:
        raise RuntimeError("Temurin JDK download was incomplete")

    extract_root = LAB_ROOT / "jdk17-extract"
    shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(JDK_DOWNLOAD, "r") as z:
        z.extractall(extract_root)

    # The archive has a versioned top-level directory such as jdk-17.0.x+N.
    candidates = [
        p for p in extract_root.iterdir()
        if p.is_dir() and (p / "bin" / ("javac.exe" if os.name == "nt" else "javac")).exists()
    ]
    if not candidates:
        raise RuntimeError("Downloaded JDK archive did not contain javac")

    src = candidates[0]
    shutil.rmtree(JDK_ROOT, ignore_errors=True)
    shutil.move(str(src), str(JDK_ROOT))
    shutil.rmtree(extract_root, ignore_errors=True)

    javac = _jdk_tool("javac")
    if not javac.exists():
        raise RuntimeError("Private JDK extraction completed but javac is still missing")

    cp = run([str(javac), "-version"], timeout=20)
    log(f"Private JDK ready: {(cp.stdout or '').strip()}")
    return JDK_ROOT


def java_tool(name: str) -> str:
    # Compilation/signing tools should use the private JDK.
    if name in {"javac", "keytool", "jarsigner"}:
        ensure_private_jdk17()
        p = _jdk_tool(name)
        if p.exists():
            return str(p)

    # Prefer private JDK for java too once installed.
    p = _jdk_tool(name)
    if p.exists():
        return str(p)

    p = _jre_tool(name)
    if p.exists():
        return str(p)

    q = shutil.which(name)
    if q:
        return q

    if name == "javac":
        ensure_private_jdk17()
        p = _jdk_tool(name)
        if p.exists():
            return str(p)

    raise RuntimeError(f"Java tool '{name}' is missing")


def android_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANDROID_HOME"] = str(SDK_ROOT)
    env["ANDROID_SDK_ROOT"] = str(SDK_ROOT)

    # Android SDK command-line tools work with the private JDK when available,
    # otherwise retain the smaller JRE used by the main Gem Bot.
    java_home = JDK_ROOT if _jdk_tool("java").exists() else JRE_ROOT
    if java_home.exists():
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    return env


def assert_isolated() -> None:
    from ldplayer_backend import instance, is_frida_test_instance, select_main_index
    main = select_main_index()
    item = instance(LDPLAYER_INDEX)
    if LDPLAYER_INDEX == main or (item is not None and not is_frida_test_instance(item)):
        raise RuntimeError("Frida requires a separate instance named KS_FRIDA_TEST; refusing main/unnamed instance")


def assert_companion_pid(pid: int) -> None:
    assert_isolated()
    if not pid or pid != _test_app_pid():
        raise RuntimeError("Refusing Frida attach: PID does not belong to com.ks.fridatest")
    cp = adb_run("shell", "cat", f"/proc/{pid}/cmdline", timeout=5)
    if (cp.stdout or "").split("\x00", 1)[0].strip() != TEST_PACKAGE:
        raise RuntimeError("Refusing Frida attach: companion process identity mismatch")


def adb_run(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    assert_isolated()
    return ld_adb(
        LDPLAYER_INDEX,
        *args,
        timeout=timeout,
        check=check,
    )


def host_frida_version() -> str:
    try:
        import frida  # type: ignore
        return str(frida.__version__)
    except Exception:
        return ""


def ensure_host_frida() -> str:
    version = host_frida_version()
    if version:
        log(f"Host Frida already installed: {version}")
        return version
    log("Installing Frida + frida-tools into the Gem Bot Python environment...")
    run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "frida", "frida-tools"], timeout=300)
    version = host_frida_version()
    if not version:
        raise RuntimeError("Frida installed but could not be imported")
    log(f"Host Frida ready: {version}")
    return version


def ensure_sdk_components() -> None:
    bt = SDK_ROOT / "build-tools" / "35.0.0"
    required = [SDK_ROOT / "platforms" / "android-35" / "android.jar",
                bt / ("d8.bat" if os.name == "nt" else "d8"),
                bt / ("aapt2.exe" if os.name == "nt" else "aapt2"),
                bt / ("apksigner.bat" if os.name == "nt" else "apksigner")]
    if all(path.is_file() for path in required):
        log("Android SDK build components already available locally")
        return
    log("Checking Android SDK components for the isolated Frida lab...")
    run([sdkmanager(), PLATFORM, BUILD_TOOLS], timeout=900, env=android_env())


def create_test_avd() -> None:
    assert_isolated()
    ensure_sdk_components()
    # Dedicated LDPlayer instance 1. Root is enabled only for this isolated test
    # instance; main RoK LDPlayer index 0 is left separate.
    ensure_ldplayer(
        LDPLAYER_INDEX,
        launch_game=False,
        root=True,
        name=LDPLAYER_NAME,
    )
    log(
        f"Frida test LDPlayer ready: index {LDPLAYER_INDEX} "
        f"({SERIAL})"
    )


def emulator_online() -> bool:
    ready, _ = ld_adb_ready(LDPLAYER_INDEX)
    return ready


def start_test_avd() -> None:
    assert_isolated()
    # Re-assert root=1 on the isolated test instance, then ensure it is online.
    ensure_ldplayer(
        LDPLAYER_INDEX,
        launch_game=False,
        root=True,
        name=LDPLAYER_NAME,
    )
    if not emulator_online():
        raise RuntimeError(
            f"LDPlayer index {LDPLAYER_INDEX} started but its ADB interface is offline. "
            "Set LDPlayer Settings > Other settings > ADB debugging to Open local connection, "
            "save, and restart that LDPlayer instance."
        )
    log(f"Frida test LDPlayer online: {SERIAL}")


def build_test_apk(gadget: Path | None = None, gadget_abi: str = "") -> Path:
    ensure_sdk_components()
    src = LAB_ROOT / "appsrc"
    classes = LAB_ROOT / "classes"
    dexout = LAB_ROOT / "dex"
    build = LAB_ROOT / "build"
    for p in (src, classes, dexout, build):
        p.mkdir(parents=True, exist_ok=True)

    package_dir = src / "com" / "ks" / "fridatest"
    package_dir.mkdir(parents=True, exist_ok=True)

    (package_dir / "TestLogic.java").write_text("""package com.ks.fridatest;
public class TestLogic {
    public static String greet(String name) { return \"Hello \" + name; }
    public static int add(int a, int b) { return a + b; }
}
""", encoding="utf-8")

    (package_dir / "FridaTestActivity.java").write_text("""package com.ks.fridatest;
import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.widget.TextView;

public class FridaTestActivity extends Activity {
    private final Handler handler = new Handler();
    private final Runnable tick = new Runnable() {
        int n = 0;
        @Override public void run() {
            n++;
            String msg = TestLogic.greet(\"Frida Lab\") + \"\\n\" +
                \"add(\" + n + \", 7) = \" + TestLogic.add(n, 7) + \"\\n\\n\" +
                \"This app exists only for the local Frida hook demo.\";
            ((TextView)findViewById(1001)).setText(msg);
            handler.postDelayed(this, 1200);
        }
    };
    @Override public void onCreate(Bundle b) {
        super.onCreate(b);
        TextView t = new TextView(this);
        t.setId(1001);
        t.setTextSize(22f);
        t.setPadding(32, 64, 32, 32);
        setContentView(t);
        handler.post(tick);
    }
    @Override public void onDestroy() {
        handler.removeCallbacks(tick);
        super.onDestroy();
    }
}
""", encoding="utf-8")

    if gadget is not None:
        activity = package_dir / "FridaTestActivity.java"
        source = activity.read_text(encoding="utf-8")
        source = source.replace("super.onCreate(b);", 'super.onCreate(b); System.loadLibrary("gemops_frida");')
        activity.write_text(source, encoding="utf-8")

    manifest = LAB_ROOT / "AndroidManifest.xml"
    manifest.write_text("""<manifest xmlns:android=\"http://schemas.android.com/apk/res/android\" package=\"com.ks.fridatest\">
  <uses-permission android:name=\"android.permission.INTERNET\" />
  <application android:extractNativeLibs=\"true\" android:theme=\"@android:style/Theme.Material.Light\" android:label=\"GemOps Companion\" android:debuggable=\"true\">
    <activity android:name=\".FridaTestActivity\" android:exported=\"true\">
      <intent-filter>
        <action android:name=\"android.intent.action.MAIN\"/>
        <category android:name=\"android.intent.category.LAUNCHER\"/>
      </intent-filter>
    </activity>
  </application>
</manifest>
""", encoding="utf-8")

    android_jar = SDK_ROOT / "platforms" / "android-35" / "android.jar"
    bt = SDK_ROOT / "build-tools" / "35.0.0"
    d8 = bt / ("d8.bat" if os.name == "nt" else "d8")
    aapt2 = bt / ("aapt2.exe" if os.name == "nt" else "aapt2")
    apksigner = bt / ("apksigner.bat" if os.name == "nt" else "apksigner")

    shutil.rmtree(classes, ignore_errors=True); classes.mkdir()
    shutil.rmtree(dexout, ignore_errors=True); dexout.mkdir()
    run([
        java_tool("javac"), "-source", "8", "-target", "8", "-classpath", str(android_jar), "-d", str(classes),
        str(package_dir / "TestLogic.java"), str(package_dir / "FridaTestActivity.java"),
    ], timeout=120, env=android_env())

    class_files = [str(p) for p in classes.rglob("*.class")]
    run([str(d8), "--lib", str(android_jar), "--output", str(dexout), *class_files], timeout=120, env=android_env())

    unsigned = build / "base-unsigned.apk"
    run([
        str(aapt2), "link", "-I", str(android_jar), "--manifest", str(manifest),
        "--min-sdk-version", "23", "--target-sdk-version", "35", "-o", str(unsigned),
    ], timeout=120, env=android_env())

    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dexout / "classes.dex", "classes.dex")
        if gadget is not None:
            z.write(gadget, f"lib/{gadget_abi}/libgemops_frida.so")
            z.writestr(f"lib/{gadget_abi}/libgemops_frida.config.so", json.dumps({
                "interaction": {"type":"listen", "address":"127.0.0.1", "port":27043,
                                "on_load":"resume", "on_port_conflict":"fail"}
            }))

    keystore = LAB_ROOT / "frida-test.keystore"
    if not keystore.exists():
        run([
            java_tool("keytool"), "-genkeypair", "-v", "-keystore", str(keystore),
            "-storepass", "android", "-keypass", "android", "-alias", "fridatest",
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "3650",
            "-dname", "CN=Local GemOps Companion,O=Kingdom Services,C=US",
        ], timeout=120, env=android_env())

    APK_PATH.unlink(missing_ok=True)
    run([
        str(apksigner), "sign", "--ks", str(keystore), "--ks-pass", "pass:android",
        "--key-pass", "pass:android", "--ks-key-alias", "fridatest",
        "--out", str(APK_PATH), str(unsigned),
    ], timeout=120, env=android_env())
    log(f"Built test APK: {APK_PATH}")
    return APK_PATH


def install_test_apk() -> None:
    start_test_avd()
    if not APK_PATH.exists():
        build_test_apk()
    cp = adb_run("install", "-r", str(APK_PATH), timeout=120, check=False)
    if cp.returncode != 0 and "success" not in (cp.stdout or "").lower():
        raise RuntimeError((cp.stdout or "").strip() or "Could not install Frida test APK")
    log("Test APK installed")


def _remove_frida_forward() -> None:
    global _FORWARD_READY, _DEVICE
    _FORWARD_READY = False
    _DEVICE = None
    try:
        adb_run(
            "forward",
            "--remove",
            f"tcp:{FRIDA_REMOTE_LOCAL_PORT}",
            timeout=10,
            check=False,
        )
    except Exception:
        pass


def _create_frida_forward() -> None:
    global _FORWARD_READY
    if _FORWARD_READY:
        return
    _remove_frida_forward()
    cp = adb_run(
        "forward",
        f"tcp:{FRIDA_REMOTE_LOCAL_PORT}",
        f"tcp:{FRIDA_REMOTE_DEVICE_PORT}",
        timeout=15,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            (cp.stdout or "").strip()
            or "Could not connect to the companion endpoint"
        )
    _FORWARD_READY = True
    log(
        "ADB Frida transport ready: "
        f"127.0.0.1:{FRIDA_REMOTE_LOCAL_PORT} -> "
        f"{SERIAL}:{FRIDA_REMOTE_DEVICE_PORT}"
    )


def frida_device():
    """
    Connect directly to the root frida-server through an ADB TCP forward.
    This avoids Frida's jailed/USB transport path.
    """
    import frida  # type: ignore
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE

    endpoint = f"127.0.0.1:{FRIDA_REMOTE_LOCAL_PORT}"
    manager = frida.get_device_manager()

    _create_frida_forward()

    try:
        _DEVICE = manager.add_remote_device(endpoint)
        return _DEVICE
    except Exception as exc:
        raise RuntimeError(
            f"Could not connect to root frida-server at {endpoint}: {exc}"
        ) from exc


def _node_executable() -> str:
    candidates = [
        PRIVATE_NODE_ROOT / "node.exe",
        PRIVATE_NODE_ROOT / "node",
    ]
    for p in candidates:
        if p.exists():
            return str(p)

    found = shutil.which("node")
    if found:
        return found

    raise RuntimeError(
        "Node.js was not found. The Frida 17 Java bridge compiler needs Node. "
        "If you previously used the Kingdom Services Appium setup, its private "
        "Node should be at %LOCALAPPDATA%\\KingdomServices\\node. Otherwise "
        "install Node.js 20+ and rerun PREPARE_FRIDA_TEST_LAB.bat."
    )


def _npm_executable() -> str:
    candidates = [
        PRIVATE_NODE_ROOT / "npm.cmd",
        PRIVATE_NODE_ROOT / "npm",
        PRIVATE_NODE_ROOT / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ]
    for p in candidates:
        if p.exists():
            if p.suffix.lower() == ".js":
                return str(p)
            return str(p)

    found = shutil.which("npm")
    if found:
        return found

    raise RuntimeError(
        "npm was not found. Node.js/npm is required to bundle the Frida 17 "
        "Java bridge agent."
    )


def _run_npm(args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    """
    Run npm from AGENT_ROOT explicitly.

    The previous build created package.json under AGENT_ROOT but inherited the
    shell's current working directory, so npm looked for package.json beside the
    extracted Gem Bot files instead.
    """
    npm = _npm_executable()
    env = os.environ.copy()
    node = _node_executable()
    node_dir = str(Path(node).parent)
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")

    AGENT_ROOT.mkdir(parents=True, exist_ok=True)

    if npm.lower().endswith(".js"):
        cmd = [node, npm, *args]
    else:
        cmd = [npm, *args]

    cp = subprocess.run(
        [str(x) for x in cmd],
        cwd=str(AGENT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        creationflags=hidden_flags(),
        env=env,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            (cp.stdout or "").strip()
            or f"npm failed with exit code {cp.returncode}"
        )
    return cp


def ensure_frida17_agent_bundle() -> Path:
    """
    Build a deterministic Frida 17 Java agent.

    Custom scripts loaded through the Python API need frida-java-bridge bundled
    explicitly on Frida 17. Pin both packages so a future npm release cannot
    silently alter this lab.
    """
    AGENT_ROOT.mkdir(parents=True, exist_ok=True)

    package_json = AGENT_ROOT / "package.json"
    package_json.write_text(
        json.dumps(
            {
                "name": "ks-frida-test-agent",
                "private": True,
                "version": "2.2.0",
                "dependencies": {
                    "frida-java-bridge": "7.0.13",
                    "frida-compile": "19.0.5",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    log(f"Frida agent npm workspace: {AGENT_ROOT}")
    log(f"Frida agent package.json: {package_json}")

    AGENT_SOURCE.write_text(
        """import Java from 'frida-java-bridge';

send({
  status: 'agent-loaded',
  pid: Process.id,
  arch: Process.arch
});

let attempts = 0;
let installing = false;
let installed = false;

function reportError(kind, error) {
  send({
    status: kind,
    attempt: attempts,
    error: String(error && (error.stack || error))
  });
}

function tryInstall() {
  if (installed || installing)
    return;

  attempts++;

  let available = false;
  try {
    available = Java.available;
  } catch (e) {
    reportError('java-probe-error', e);
  }

  send({
    status: 'java-probe',
    attempt: attempts,
    available: available
  });

  if (!available) {
    if (attempts < 80)
      setTimeout(tryInstall, 250);
    else
      send({status: 'hook-error', error: 'Java runtime never became available'});
    return;
  }

  installing = true;

  try {
    Java.perform(function () {
      try {
        send({
          status: 'java-ready',
          attempt: attempts,
          androidVersion: Java.androidVersion
        });

        const Logic = Java.use('com.ks.fridatest.TestLogic');

        const greet = Logic.greet.overload('java.lang.String');
        greet.implementation = function (name) {
          const result = greet.call(this, name);
          send({
            method: 'greet',
            args: [String(name)],
            result: String(result)
          });
          return result;
        };

        const add = Logic.add.overload('int', 'int');
        add.implementation = function (a, b) {
          const result = add.call(this, a, b);
          send({
            method: 'add',
            args: [a, b],
            result: result
          });
          return result;
        };

        installed = true;
        installing = false;

        send({
          status: 'hooks-installed',
          attempt: attempts,
          androidVersion: Java.androidVersion
        });
      } catch (e) {
        installing = false;
        reportError('hook-install-retry', e);
        if (attempts < 80)
          setTimeout(tryInstall, 250);
        else
          reportError('hook-error', e);
      }
    });
  } catch (e) {
    installing = false;
    reportError('java-perform-retry', e);
    if (attempts < 80)
      setTimeout(tryInstall, 250);
    else
      reportError('hook-error', e);
  }
}

setTimeout(tryInstall, 0);
""",
        encoding="utf-8",
    )

    node_modules = AGENT_ROOT / "node_modules"
    bridge_dir = node_modules / "frida-java-bridge"
    compiler_dir = node_modules / "frida-compile"

    log("Checking pinned Frida Java bridge/compiler packages...")
    _run_npm(
        ["install", "--no-fund", "--no-audit", "--omit=optional"],
        timeout=900,
    )

    if not bridge_dir.exists() or not compiler_dir.exists():
        raise RuntimeError(
            "npm completed but the pinned Frida bridge/compiler packages are missing"
        )

    possible = [
        node_modules / ".bin" / "frida-compile.cmd",
        node_modules / ".bin" / "frida-compile",
        compiler_dir / "bin" / "frida-compile.js",
    ]
    compiler = next((p for p in possible if p.exists()), None)
    if compiler is None:
        raise RuntimeError("Local frida-compile executable was not found")

    env = os.environ.copy()
    node = _node_executable()
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")

    compile_args = [
        str(AGENT_SOURCE),
        "-o", str(AGENT_BUNDLE),
        "-S",
        "-c",
        "-B", "iife",
        "-T", "none",
    ]
    if compiler.suffix.lower() == ".js":
        cmd = [node, str(compiler), *compile_args]
    else:
        cmd = [str(compiler), *compile_args]

    cp = subprocess.run(
        [str(x) for x in cmd],
        cwd=str(AGENT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=240,
        creationflags=hidden_flags(),
        env=env,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            "frida-compile failed:\n"
            + ((cp.stdout or "").strip() or f"exit code {cp.returncode}")
        )

    if not AGENT_BUNDLE.exists() or AGENT_BUNDLE.stat().st_size < 1000:
        raise RuntimeError(
            "frida-compile completed but did not produce a usable bundled agent"
        )

    log(
        f"Frida 17 Java bridge agent bundled: {AGENT_BUNDLE} "
        f"({AGENT_BUNDLE.stat().st_size:,} bytes)"
    )
    return AGENT_BUNDLE


def _test_app_pid() -> int:
    cp = adb_run(
        "shell",
        "pidof",
        TEST_PACKAGE,
        timeout=6,
        check=False,
    )
    raw = (cp.stdout or "").strip()
    if not raw:
        return 0
    try:
        return int(raw.split()[0])
    except Exception:
        return 0


def _start_test_app_ready() -> int:
    """
    Start the bundled test activity normally and wait for a stable process.
    This deliberately avoids the suspended-spawn/ART race.
    """
    adb_run(
        "shell",
        "am",
        "force-stop",
        TEST_PACKAGE,
        timeout=10,
        check=False,
    )

    cp = adb_run(
        "shell",
        "am",
        "start",
        "-W",
        "-n",
        TEST_ACTIVITY,
        timeout=30,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            "Could not start the Frida test app: "
            + ((cp.stdout or "").strip() or f"exit code {cp.returncode}")
        )

    deadline = time.monotonic() + 20
    pid = 0
    stable_since = 0.0

    while time.monotonic() < deadline:
        now_pid = _test_app_pid()
        if now_pid:
            if now_pid != pid:
                pid = now_pid
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 1.25:
                log(f"Test app ready as PID {pid}")
                return pid
        time.sleep(0.25)

    raise RuntimeError(
        "Test app Activity launched, but its process never became stable"
    )


def _collect_test_diagnostics() -> str:
    parts: list[str] = []

    try:
        cp = adb_run(
            "shell",
            "dumpsys",
            "activity",
            "activities",
            timeout=12,
            check=False,
        )
        text = (cp.stdout or "").strip()
        if text:
            interesting = [
                line
                for line in text.splitlines()
                if TEST_PACKAGE in line or "mResumedActivity" in line
            ]
            if interesting:
                parts.append("activity:\n" + "\n".join(interesting[-20:]))
    except Exception:
        pass

    try:
        cp = adb_run(
            "logcat",
            "-d",
            "-t",
            "120",
            timeout=15,
            check=False,
        )
        text = (cp.stdout or "").strip()
        if text:
            lines = [
                line
                for line in text.splitlines()
                if TEST_PACKAGE in line
                or "frida" in line.lower()
                or "AndroidRuntime" in line
            ]
            if lines:
                parts.append("logcat:\n" + "\n".join(lines[-50:]))
    except Exception:
        pass

    return "\n\n".join(parts)[-7000:]


def prepare_companion_gadget():
    """Build Frida INTO our own test app; endpoint exposes this process only."""
    global FRIDA_REMOTE_DEVICE_PORT, _DEVICE
    import frida
    assert_isolated()
    LAB_LOG.write_text("", encoding="utf-8")
    version = ensure_host_frida()
    create_test_avd()
    ensure_private_jdk17()
    abi = (adb_run("shell", "getprop", "ro.product.cpu.abi", timeout=8).stdout or "").strip()
    frida_abi = {"x86_64":"x86_64", "x86":"x86", "arm64-v8a":"arm64", "armeabi-v7a":"arm"}.get(abi)
    if not frida_abi:
        raise RuntimeError("Unsupported companion ABI: " + abi)
    asset = f"frida-gadget-{version}-android-{frida_abi}.so.xz"
    cache = LAB_ROOT / asset
    if not cache.exists():
        log("Downloading matching Frida Gadget for the companion: " + asset)
        with urllib.request.urlopen(f"https://github.com/frida/frida/releases/download/{version}/{asset}", timeout=90) as response:
            cache.write_bytes(response.read())
    gadget = LAB_ROOT / "libgemops_frida.so"
    gadget.write_bytes(lzma.decompress(cache.read_bytes()))
    build_test_apk(gadget, abi)
    install_test_apk()
    ensure_frida17_agent_bundle()
    # A distinct guest port means an old diagnostic server cannot become this endpoint.
    _remove_frida_forward()
    FRIDA_REMOTE_DEVICE_PORT = 27043
    pid = _start_test_app_ready()
    assert_companion_pid(pid)
    last_error = ''
    for attempt in range(3):
        try:
            device = frida_device()
            processes = device.enumerate_processes()
            if len(processes) != 1 or processes[0].pid != pid:
                raise RuntimeError("Embedded Frida must expose only the companion PID")
            log(f"Companion-only Frida Gadget endpoint verified: PID {pid}, version {version}")
            return device, pid
        except Exception as exc:
            last_error = str(exc)
            try:
                frida.get_device_manager().remove_remote_device(f"127.0.0.1:{FRIDA_REMOTE_LOCAL_PORT}")
            except Exception:
                pass
            _remove_frida_forward()
            time.sleep(1)
    raise RuntimeError("Companion Gadget connection failed: " + last_error + "\n" + _collect_test_diagnostics())


