"""Fail-closed local health gate shared by all GemBot input paths."""
import json
import os
import time
import urllib.request


class RuntimeHealthError(RuntimeError):
    pass


class RuntimeGuard:
    def __init__(self):
        self.url = os.environ.get('GEMOPS_GATE_URL', '')
        self.token = os.environ.get('GEMOPS_GATE_TOKEN', '')

    def require(self):
        if not self.url or not self.token:
            raise RuntimeHealthError('Start GemOps through Nexus: a live Frida companion is required')
        try:
            request = urllib.request.Request(self.url, headers={'X-GemOps-Gate': self.token})
            with urllib.request.urlopen(request, timeout=2) as response:
                data = json.load(response)
            if not data.get('passed'):
                raise RuntimeHealthError(data.get('error') or 'Required companion health lost')
        except RuntimeHealthError:
            raise
        except Exception as exc:
            raise RuntimeHealthError('Required companion gate unavailable: ' + str(exc)) from exc

    def analyze_map(self, png):
        self.require()
        request = urllib.request.Request(self.url.rsplit('/', 1)[0] + '/map', data=png,
            headers={'X-GemOps-Gate': self.token, 'Content-Type': 'image/png'})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)


class StateWatchdog:
    """State residence deadlines; repeated reporting cannot reset a deadline."""
    LIMITS = {'leave_city': 15, 'builtin_gem_finder': 45, 'ai_action_wait': 35, 'gather_button': 35,
              'new_troop': 40, 'march': 35, 'gem_flow_recovery': 60, 'find_gem': 245}
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = 'starting'
        self.since = clock()
        self.transitions = []
    def enter(self, state):
        if state != self.state:
            self.transitions.append({'from': self.state, 'to': state})
            self.transitions = self.transitions[-16:]
            self.state, self.since = state, self.clock()
    def snapshot(self):
        age = max(0, self.clock() - self.since)
        limit = self.LIMITS.get(self.state)
        return dict(machine_state=self.state, state_age_seconds=round(age, 1),
                    state_deadline_seconds=limit, watchdog_overdue=bool(limit and age > limit),
                    state_transitions=list(self.transitions))
