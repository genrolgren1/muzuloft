"""Dashboard-owned companion service; automatic startup, bounded recovery."""
import threading
import time


class CompanionService:
    def __init__(self, runtime, main_index, stop_mission, event, clock=time.monotonic):
        self.runtime, self.main_index = runtime, main_index
        self.stop_mission, self.event, self.clock = stop_mission, event, clock
        self.lock = threading.RLock()
        self.enabled = True
        self.attempts = 0
        self.next_at = 0
        self.started_at = None
        self.healthy_since = None
        self.error = ''

    def snapshot(self):
        with self.lock:
            return dict(automatic=self.enabled, attempts=self.attempts,
                        retry_in_seconds=max(0, round(self.next_at-self.clock())),
                        service_error=self.error, service_mode='background')

    def start(self, restart=False):
        with self.lock:
            if restart:
                self.stop_mission()
                self.runtime.stop()
            self.enabled = True
            self.attempts = 0
            self.next_at = 0
            self.started_at = None
            self.healthy_since = None
            self.error = ''

    def stop(self):
        with self.lock:
            # A deliberate Stop must never be undone by automatic recovery.
            self.enabled = False
            self.stop_mission()
            self.runtime.stop()
            self.started_at = None
            self.healthy_since = None
            self.error = 'Service paused until Resume or the next Nexus launch'

    def tick(self):
        with self.lock:
            if not self.enabled:
                return
            now = self.clock()
            data = self.runtime.snapshot()
            main = self.main_index()
            if data.get('worker_alive') and data.get('main_index') != main:
                self.stop()
                self.error = 'Main emulator changed; resume the companion service for the selected instance'
                self.event(self.error)
                return
            if data.get('passed'):
                if self.healthy_since is None:
                    self.healthy_since = now
                    self.event('Frida companion service is operational; mission gate open')
                if now-self.healthy_since >= 60:
                    self.attempts = 0
                self.error = ''
                return
            self.healthy_since = None
            if data.get('worker_alive') and data.get('state') in {'STARTING','PREPARING','VERIFYING'}:
                if self.started_at is None:
                    self.started_at = now
                if now-self.started_at < 900:
                    return
                self.error = 'Companion preparation exceeded 15 minutes'
            if data.get('worker_alive'):
                self.stop_mission()
                self.runtime.stop()
            if self.started_at is not None:
                self.stop_mission()
                self.started_at = None
                self.next_at = now + (15, 60, 120)[min(max(self.attempts-1, 0), 2)]
                self.event('Companion service lost health; mission stopped; recovery scheduled')
            if now < self.next_at:
                return
            if self.attempts >= 3:
                self.enabled = False
                self.error = 'Automatic recovery paused after three unsuccessful starts; inspect Companion, then Resume'
                self.event(self.error)
                return
            self.attempts += 1
            self.started_at = now
            try:
                self.runtime.start(main)
                self.event('Frida companion service starting automatically')
            except Exception as exc:
                self.error = str(exc)
                self.event('Companion service could not start: ' + self.error)
                # Next tick schedules the bounded retry; never spin on failure.

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self.enabled = False
                    self.error = str(exc)
                self.stop_mission()
                self.event('Companion service paused: ' + str(exc))
            time.sleep(1)
