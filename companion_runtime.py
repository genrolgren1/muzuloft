"""Required, continuously instrumented companion. No configurable attach target."""
from __future__ import annotations

import json
import base64
import io
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

PACKAGE = "com.ks.fridatest"
MARKER = "@@COMPANION@@"
MAP_MARKER = "@@COMPANION_MAP@@"


def choose_lab_index(items, main_index):
    labs = [int(x['index']) for x in items if str(x.get('name', '')).upper() == 'KS_FRIDA_TEST'
            and int(x['index']) != main_index]
    return min(labs) if labs else max([main_index] + [int(x['index']) for x in items]) + 1


class CompanionRuntime:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.process = None
        self.updated = 0.0
        self.state = {'state': 'STOPPED', 'error': 'Start the required companion first'}
        self.logs = deque(maxlen=100)
        self.generation = ''
        self.main_index = None
        self.map_lock = threading.Lock()
        self.map_pending = {}
        self.map_requests = 0
        self.map_last_ms = None

    def snapshot(self):
        with self.lock:
            data = dict(self.state)
            alive = self.process is not None and self.process.poll() is None
            age = time.monotonic() - self.updated if self.updated else None
            passed = bool(alive and age is not None and age < 6 and data.get('state') == 'PASS'
                          and data.get('java') and data.get('native') and data.get('package') == PACKAGE)
            if data.get('state') == 'PASS' and not passed:
                data.update(state='FAIL', error='Companion heartbeat expired or worker exited')
            if data.get('state') in {'FAIL', 'STOPPED'}:
                data.update(java=False, native=False, pid=None)
            data.update(required=True, passed=passed, worker_alive=alive, map_requests=self.map_requests, map_last_ms=self.map_last_ms,
                        heartbeat_age_seconds=round(age, 1) if age is not None else None,
                        main_index=self.main_index, log_tail='\n'.join(self.logs))
            return data

    def require(self, main_index=None):
        data = self.snapshot()
        if not data['passed'] or (main_index is not None and main_index != self.main_index):
            raise RuntimeError('FRIDA REQUIRED: companion health must PASS for this RoK instance. '
                               + str(data.get('error') or data.get('state')))

    def start(self, main_index):
        from ldplayer_backend import list_instances, is_frida_test_instance
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                if main_index != self.main_index:
                    raise RuntimeError('Stop companion before changing the main LDPlayer')
                return self.snapshot()
            items = list_instances()
            if any(int(x['index']) == main_index and is_frida_test_instance(x) for x in items):
                raise RuntimeError('The main instance cannot be KS_FRIDA_TEST')
            index = choose_lab_index(items, main_index)
            self.generation = secrets.token_hex(16)
            self.main_index = main_index
            self.updated = 0.0
            self.logs.clear()
            self.state = {'state': 'STARTING', 'package': PACKAGE, 'lab_index': index, 'error': ''}
            env = dict(os.environ, FRIDA_LDPLAYER_INDEX=str(index), KS_LD_INDEX=str(main_index),
                       KS_LD_HEADLESS='1', GEMOPS_COMPANION_GENERATION=self.generation)
            self.process = subprocess.Popen([sys.executable, '-u', str(self.root / 'companion_runtime.py'), '--worker'],
                cwd=str(self.root), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            threading.Thread(target=self._read, args=(self.process, self.generation), daemon=True).start()
            return self.snapshot()

    def _read(self, process, generation):
        for line in process.stdout:
            with self.lock:
                if generation != self.generation:
                    return
                if line.startswith(MAP_MARKER):
                    try:
                        reply = json.loads(line[len(MAP_MARKER):])
                        pending = self.map_pending.get(reply.get('id'))
                        if pending is not None:
                            pending['reply'] = reply
                            pending['event'].set()
                    except (ValueError, TypeError):
                        self.logs.append('Malformed companion map reply')
                elif line.startswith(MARKER):
                    try:
                        data = json.loads(line[len(MARKER):])
                        if data.pop('generation', '') == generation:
                            self.state.update(data)
                            self.updated = time.monotonic()
                    except (ValueError, TypeError):
                        self.logs.append('Malformed companion heartbeat')
                else:
                    self.logs.append(line.rstrip()[-800:])
        with self.lock:
            if generation == self.generation and self.state.get('state') != 'STOPPED':
                self.state.update(state='FAIL', error=self.state.get('error') or 'Companion worker exited; use Restart companion')

    def analyze_map(self, png, timeout=2.0):
        from PIL import Image
        if len(png) > 3_000_000:
            raise ValueError('Screenshot exceeds 3 MB')
        self.require()
        if not self.map_lock.acquire(blocking=False):
            raise RuntimeError('Companion scanner is busy')
        request_id = secrets.token_hex(8)
        t0 = time.monotonic()
        try:
            with Image.open(io.BytesIO(png)) as image:
                if image.width > 4096 or image.height > 4096:
                    raise ValueError('Screenshot dimensions exceed limit')
                image.thumbnail((320, 320))
                rgb = image.convert('RGB')
                command = dict(id=request_id, width=rgb.width, height=rgb.height,
                               pixels=base64.b64encode(rgb.tobytes()).decode('ascii'))
            pending = dict(event=threading.Event())
            with self.lock:
                self.require()
                self.map_pending[request_id] = pending
                self.process.stdin.write(json.dumps(command) + '\n')
                self.process.stdin.flush()
            if not pending['event'].wait(timeout):
                raise TimeoutError('Companion map scan timed out')
            reply = pending['reply']
            if reply.get('error'):
                raise RuntimeError(reply['error'])
            self.require()
            with self.lock:
                self.map_requests += 1
                self.map_last_ms = round((time.monotonic()-t0)*1000, 1)
            return {**reply['result'], 'elapsed_ms': self.map_last_ms}
        finally:
            with self.lock:
                self.map_pending.pop(request_id, None)
            self.map_lock.release()

    def stop(self):
        with self.lock:
            self.generation = secrets.token_hex(16)
            process = self.process
            self.process = None
            for pending in self.map_pending.values():
                pending['reply'] = {'error': 'Companion stopped'}
                pending['event'].set()
            self.state.update(state='STOPPED', error='Companion stopped', java=False, native=False, pid=None)
        if process and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def worker():
    import companion_setup as lab
    output_lock = threading.Lock()
    def report(**data):
        with output_lock:
            print(MARKER + json.dumps(dict(data, generation=os.environ['GEMOPS_COMPANION_GENERATION'],
              package=PACKAGE, lab_index=lab.LDPLAYER_INDEX)), flush=True)
    session = None
    try:
        lab.assert_isolated()
        report(state='PREPARING', error='')
        # Existing setup installs/builds only the dedicated test app and its tools.
        device, pid = lab.prepare_companion_gadget()
        lab.assert_isolated()
        lab.assert_companion_pid(pid)
        session = device.attach(pid)
        signal_lock = threading.Lock()
        signals = {'java_at': 0.0, 'native_at': 0.0, 'error': '', 'events': 0}
        def detached(*args):
            with signal_lock:
                signals['error'] = 'Companion Frida session detached'
        session.on('detached', detached)
        def message(msg, data):
            with signal_lock:
                p = msg.get('payload', {})
                if msg.get('type') == 'error':
                    signals['error'] = str(msg.get('description', 'Agent error'))
                elif isinstance(p, dict):
                    if p.get('method') in {'greet', 'add'}:
                        signals['java_at'] = time.monotonic()
                        signals['events'] += 1
                    if p.get('status') == 'runtime-native' and p.get('pid') == pid:
                        signals['native_at'] = time.monotonic()
                        signals['events'] += 1
                    if p.get('status') in {'hook-error', 'runtime-native-error'}:
                        signals['error'] = str(p.get('error'))
        java = session.create_script(lab.AGENT_BUNDLE.read_text(encoding='utf-8'))
        java.on('message', message)
        java.load()
        native = session.create_script('''
          const target = Module.getGlobalExportByName('getpid');
          let hits = 0;
          Interceptor.attach(target, {onEnter() { hits++; }});
          Interceptor.flush();
          const call = new NativeFunction(target, 'int', []);
          setInterval(() => {
            try { const before = hits; const pid = call();
              if (hits > before) send({status:'runtime-native', pid:pid});
            } catch(e) { send({status:'runtime-native-error', error:String(e)}); }
          }, 1000);
        ''')
        native.on('message', message)
        native.load()
        scanner = session.create_script((Path(__file__).parent/'companion_map.js').read_text(encoding='utf-8'))
        scanner.load()
        def scan_requests():
            for line in sys.stdin:
                request_id = None
                try:
                    if len(line) > 420000:
                        raise ValueError('Scanner request too large')
                    command = json.loads(line)
                    request_id = command['id']
                    result = scanner.exports_sync.scanmap(command['pixels'], command['width'], command['height'])
                    reply = dict(id=request_id, result=result)
                except Exception as exc:
                    reply = dict(id=request_id, error=str(exc))
                with output_lock:
                    print(MAP_MARKER + json.dumps(reply), flush=True)
        threading.Thread(target=scan_requests, daemon=True).start()
        start = time.monotonic()
        while True:
            now = time.monotonic()
            with signal_lock:
                s = dict(signals)
            j, n = now - s['java_at'] < 6, now - s['native_at'] < 6
            if s['error'] or (now - start > 30 and not (j and n)):
                raise RuntimeError(s['error'] or 'Java/native hook events stopped')
            report(state='PASS' if j and n else 'VERIFYING', java=j, native=n, pid=pid,
                   version=lab.host_frida_version(), transport='embedded companion / loopback', events=s['events'], error='',
                   last_event_age_seconds=round(now - max(s['java_at'], s['native_at']), 2))
            time.sleep(1)
    except Exception as exc:
        report(state='FAIL', java=False, native=False, error=str(exc))
        return 1
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(worker())
