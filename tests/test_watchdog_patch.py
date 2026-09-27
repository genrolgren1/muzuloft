import time
import unittest
from unittest.mock import Mock, patch
import test_reliability as base
from gem_bot import Vision
from runtime_guard import StateWatchdog


class WatchdogPatchTests(unittest.TestCase):
    def test_action_request_timeout_is_shorter_than_heartbeat_limit(self):
        vision=Vision.__new__(Vision);vision.model='test';vision._resize=Mock(return_value=(b'image',1280,720))
        with patch('gem_bot.urllib.request.urlopen',side_effect=TimeoutError('slow model')) as request:
            with self.assertRaises(TimeoutError):vision.next_action(b'image','test')
        self.assertEqual(request.call_args.kwargs['timeout'],25)

    def test_ai_timeout_publishes_wait_and_exit_without_input(self):
        b=base.BotTests().bot();b._vision=Mock()
        b._vision.return_value.next_action.side_effect=TimeoutError('slow model')
        tasks=[];b.status.side_effect=lambda **kw:tasks.append(b.last_task)
        self.assertFalse(b.perform_goal('login_recovery','test'))
        self.assertEqual(tasks,['login_recovery','ai_action_wait','login_recovery'])
        self.assertEqual(b.last_failure_code,'optional_ai_unavailable')
        b.adb.tap.assert_not_called();b.adb.swipe.assert_not_called();b.adb.back.assert_not_called()

    def test_ai_success_restores_original_task(self):
        b=base.BotTests().bot();b._vision=Mock()
        b._vision.return_value.next_action.return_value={'status':'done'}
        self.assertTrue(b.perform_goal('login_recovery','test'))
        self.assertEqual(b.last_task,'login_recovery')

    def test_known_game_controls_skip_startup_ai(self):
        b=base.BotTests().bot();b.start_game_gate=Mock();b.perform_goal=Mock()
        b._best_template_hit=Mock(return_value=('tree_action_reference',{}))
        self.assertTrue(b.recover_login_gate(startup=True))
        b.perform_goal.assert_not_called();self.assertEqual(b.last_task,'game_ready')

    def test_missing_tree_publishes_finder_exit(self):
        b=base.BotTests().bot();b._finder_after=0
        b._best_template_hit=Mock(return_value=None)
        tasks=[];b.status.side_effect=lambda **kw:tasks.append(b.last_task)
        self.assertFalse(b.prepare_builtin_gem_search())
        self.assertEqual(tasks,['builtin_gem_finder','find_gem'])

    def test_exception_does_not_leave_stale_finder_state(self):
        b=base.BotTests().bot();b._finder_after=0
        b.adb.screenshot.side_effect=RuntimeError('device lost')
        with self.assertRaisesRegex(RuntimeError,'device lost'):b.prepare_builtin_gem_search()
        self.assertEqual(b.last_task,'find_gem')

    def test_expired_finder_cannot_hold_or_select(self):
        b=base.BotTests().bot();b._finder_after=0;b._finder_deadline=0
        b._best_template_hit=Mock(return_value=('tree',dict(cx=1,cy=2)))
        self.assertFalse(b._prepare_builtin_gem_search_impl())
        b.adb.swipe.assert_not_called();b.adb.tap.assert_not_called()
        self.assertGreater(b._finder_after,time.monotonic())

    def test_finder_budget_cannot_extend_mission_deadline(self):
        b=base.BotTests().bot();b._cycle_deadline=time.monotonic()+5
        b._prepare_builtin_gem_search_impl=Mock(return_value=False)
        b.prepare_builtin_gem_search()
        self.assertEqual(b._finder_deadline,b._cycle_deadline)

    def test_real_stalls_still_expire_despite_status_updates(self):
        clock=Mock(return_value=0)
        for task,limit in [('ai_action_wait',35),('builtin_gem_finder',45)]:
            clock.return_value=0;w=StateWatchdog(clock);w.enter(task)
            clock.return_value=limit-1;w.enter(task)
            self.assertFalse(w.snapshot()['watchdog_overdue'])
            clock.return_value=limit+1;w.enter(task)
            self.assertTrue(w.snapshot()['watchdog_overdue'])


if __name__=='__main__':unittest.main(verbosity=2)
