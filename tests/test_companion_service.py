import unittest
from unittest.mock import Mock
from companion_service import CompanionService


class ServiceTests(unittest.TestCase):
    def service(self):
        self.clock=Mock(return_value=0)
        self.runtime=Mock()
        self.state=dict(state='STOPPED',passed=False,worker_alive=False,main_index=0)
        self.runtime.snapshot.side_effect=lambda:dict(self.state)
        self.stop=Mock();self.event=Mock()
        return CompanionService(self.runtime,lambda:0,self.stop,self.event,self.clock)

    def test_initial_tick_starts_companion_automatically(self):
        s=self.service();s.tick();self.runtime.start.assert_called_once_with(0)
        self.assertTrue(s.snapshot()['automatic'])

    def test_preparing_does_not_spawn_another_worker(self):
        s=self.service();s.tick();self.state.update(state='PREPARING',worker_alive=True)
        self.clock.return_value=400;s.tick();self.runtime.start.assert_called_once()

    def test_hook_verification_is_startup_not_health_loss(self):
        s=self.service();s.tick();self.state.update(state='VERIFYING',worker_alive=True)
        self.clock.return_value=2;s.tick()
        self.runtime.stop.assert_not_called();self.stop.assert_not_called()
        self.runtime.start.assert_called_once()

    def test_pause_stays_stopped_across_ticks(self):
        s=self.service();s.stop();self.runtime.reset_mock()
        for t in (1,10,1000):self.clock.return_value=t;s.tick()
        self.runtime.start.assert_not_called();self.assertFalse(s.snapshot()['automatic'])

    def test_resume_reenables_automatic_start(self):
        s=self.service();s.stop();s.start();s.tick()
        self.runtime.start.assert_called_once();self.assertTrue(s.snapshot()['automatic'])

    def test_dead_worker_stops_mission_before_delayed_recovery(self):
        s=self.service();s.tick();self.state.update(state='FAIL')
        self.clock.return_value=2;s.tick();self.stop.assert_called_once()
        self.assertEqual(self.runtime.start.call_count,1)
        self.clock.return_value=17;s.tick();self.assertEqual(self.runtime.start.call_count,2)

    def test_recovery_stops_after_three_failures(self):
        s=self.service();self.runtime.start.side_effect=RuntimeError('unavailable')
        for t in (0,1,16,17,77,78,198):self.clock.return_value=t;s.tick()
        self.assertEqual(self.runtime.start.call_count,3)
        self.assertFalse(s.snapshot()['automatic'])
        self.assertIn('three',s.snapshot()['service_error'])

    def test_sustained_health_resets_retry_budget(self):
        s=self.service();s.attempts=3
        self.state.update(state='PASS',worker_alive=True,passed=True)
        s.tick();self.clock.return_value=61;s.tick()
        self.assertEqual(s.attempts,0);self.runtime.start.assert_not_called()

    def test_main_instance_change_pauses_service(self):
        s=self.service();self.state.update(state='PASS',worker_alive=True,passed=True,main_index=2)
        s.tick();self.assertFalse(s.enabled);self.stop.assert_called_once()
        self.runtime.stop.assert_called_once();self.runtime.start.assert_not_called()

    def test_preparation_has_bounded_deadline(self):
        s=self.service();s.tick();self.state.update(state='PREPARING',worker_alive=True)
        self.clock.return_value=901;s.tick()
        self.runtime.stop.assert_called_once();self.assertFalse(s.runtime.start.call_count>1)


if __name__=='__main__':unittest.main(verbosity=2)
