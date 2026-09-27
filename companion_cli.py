"""Use Nexus as the single owner of the mandatory Frida runtime."""
import json
import re
import sys
import time
import urllib.request

def main():
    base = 'http://127.0.0.1:8080'
    command = sys.argv[1] if len(sys.argv) > 1 else 'doctor'
    with urllib.request.urlopen(base + '/', timeout=5) as response:
        html = response.read().decode()
    token = re.search(r'name="account-token" content="([^"]+)"', html).group(1)
    if command == 'stop':
        stop_bot = urllib.request.Request(base + '/api/bot/stop', data=b'{}',
            headers={'Content-Type':'application/json', 'X-Account-Token':token})
        with urllib.request.urlopen(stop_bot, timeout=15) as response:
            print(response.read().decode())
    action = 'stop' if command == 'stop' else 'restart'
    request = urllib.request.Request(base + '/api/frida/' + action, data=b'{}',
        headers={'Content-Type': 'application/json', 'X-Account-Token': token})
    with urllib.request.urlopen(request, timeout=15) as response:
        print(response.read().decode())
    if command == 'stop':
        return 0
    deadline = time.monotonic() + 900
    last = ''
    while time.monotonic() < deadline:
        with urllib.request.urlopen(base + '/api/frida/status', timeout=5) as response:
            state = json.load(response)
        if state.get('state') != last:
            print(state.get('state'), state.get('error', ''), flush=True)
            last = state.get('state')
        if state.get('passed'):
            print('PASS: persistent native and Java hooks active in com.ks.fridatest only')
            return 0
        if state.get('state') in {'FAIL', 'STOPPED'}:
            return 1
        time.sleep(2)
    print('FAIL: companion setup timed out; inspect Nexus companion log')
    return 1

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('FAIL:', exc, '\nStart RUN_GEM_BOT.bat and inspect the Companion page.')
        raise SystemExit(1)
