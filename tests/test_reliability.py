"""Offline regressions: no emulator, account changes or gameplay input."""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Keep test configuration out of the installed user's state.
_test_home = tempfile.TemporaryDirectory(prefix='gemops-tests-')
os.environ['KS_HOME'] = _test_home.name
os.environ.pop('KS_LD_INDEX', None)

from companion_runtime import CompanionRuntime, choose_lab_index, PACKAGE
from runtime_guard import RuntimeGuard, RuntimeHealthError, StateWatchdog
from session_metrics import SessionMetrics
from gem_bot import GemBot, ADB
import companion_setup as frida_lab
import web_server as web


class CompanionTests(unittest.TestCase):
    def healthy(self):
        c = CompanionRuntime(Path(__file__).resolve().parents[1])
        c.process = Mock(); c.process.poll.return_value = None
        c.updated = time.monotonic()
        c.main_index = 3
        c.state = dict(state='PASS', java=True, native=True, package=PACKAGE)
        return c

    def test_pass_requires_both_live_hooks(self):
        c = self.healthy(); self.assertTrue(c.snapshot()['passed'])
        for key in ('java', 'native'):
            c = self.healthy(); c.state[key] = False
            self.assertFalse(c.snapshot()['passed'])

    def test_stale_pass_is_fail(self):
        c = self.healthy(); c.updated -= 7
        self.assertFalse(c.snapshot()['passed']); self.assertEqual(c.snapshot()['state'], 'FAIL')
        self.assertFalse(c.snapshot()['native']); self.assertFalse(c.snapshot()['java'])

    def test_dead_worker_invalidates_fresh_pass(self):
        c=self.healthy(); c.process.poll.return_value=1
        self.assertFalse(c.snapshot()['passed'])

    def test_wrong_package_never_passes(self):
        c=self.healthy(); c.state['package']='com.lilithgame.roc.gp'
        self.assertFalse(c.snapshot()['passed'])

    def test_gate_bound_to_main_instance(self):
        c=self.healthy(); c.require(3)
        with self.assertRaises(RuntimeError): c.require(4)

    def test_select_reuses_only_named_lab(self):
        items=[dict(index=0,name='RoK'),dict(index=1,name='Another account'),dict(index=4,name='KS_FRIDA_TEST')]
        self.assertEqual(choose_lab_index(items,0),4)
        self.assertEqual(choose_lab_index(items[:2],0),2)
        self.assertEqual(choose_lab_index(items,4),5)

    def test_main_index_is_forbidden_to_lab(self):
        with patch('ldplayer_backend.select_main_index', return_value=1), patch('ldplayer_backend.instance', return_value=dict(name='KS_FRIDA_TEST')), patch.object(frida_lab,'LDPLAYER_INDEX',1):
            with self.assertRaises(RuntimeError): frida_lab.assert_isolated()

    def test_normal_named_instance_is_forbidden_to_lab(self):
        with patch('ldplayer_backend.select_main_index', return_value=0), patch('ldplayer_backend.instance', return_value=dict(name='Other account')), patch.object(frida_lab,'LDPLAYER_INDEX',1):
            with self.assertRaises(RuntimeError): frida_lab.assert_isolated()

    def test_pid_cmdline_must_match_companion(self):
        with patch.object(frida_lab,'assert_isolated'), patch.object(frida_lab,'_test_app_pid',return_value=42), patch.object(frida_lab,'adb_run',return_value=Mock(stdout='com.lilithgame.roc.gp\x00')):
            with self.assertRaises(RuntimeError): frida_lab.assert_companion_pid(42)

    def test_pid_must_match_current_companion(self):
        with patch.object(frida_lab,'assert_isolated'), patch.object(frida_lab,'_test_app_pid',return_value=43):
            with self.assertRaises(RuntimeError): frida_lab.assert_companion_pid(42)

    def test_correct_companion_identity(self):
        with patch.object(frida_lab,'assert_isolated'), patch.object(frida_lab,'_test_app_pid',return_value=42), patch.object(frida_lab,'adb_run',return_value=Mock(stdout=PACKAGE+'\x00')):
            frida_lab.assert_companion_pid(42)


class GateTests(unittest.TestCase):
    def test_adb_resolution_never_restarts_shared_server(self):
        import ldplayer_backend as ld
        with patch.dict(ld._SERIAL_CACHE,{},clear=True), patch.object(ld,'_wait_android_started'), patch.object(ld,'find_adb',return_value='adb'), patch.object(ld,'run') as command, patch.object(ld,'_adb_devices',return_value=['emulator-5556']),patch.object(ld,'_restart_adb_server') as restart:
            self.assertEqual(ld.resolve_adb_serial(1),'emulator-5556')
            restart.assert_not_called()
            command.assert_called_once_with(['adb','start-server'],timeout=10)

    def test_existing_frida_forward_is_not_reset(self):
        with patch.object(frida_lab,'_FORWARD_READY',True),patch.object(frida_lab,'_remove_frida_forward') as remove,patch.object(frida_lab,'adb_run') as adb:
            frida_lab._create_frida_forward();remove.assert_not_called();adb.assert_not_called()

    def test_direct_cli_fails_closed(self):
        with patch.dict(os.environ, {'GEMOPS_GATE_URL':'','GEMOPS_GATE_TOKEN':''}):
            with self.assertRaises(RuntimeHealthError): RuntimeGuard().require()

    def test_http_failure_fails_closed(self):
        with patch.dict(os.environ, {'GEMOPS_GATE_URL':'http://127.0.0.1/gate','GEMOPS_GATE_TOKEN':'test'}), patch('urllib.request.urlopen',side_effect=OSError('offline')):
            with self.assertRaises(RuntimeHealthError): RuntimeGuard().require()

    def test_no_adb_input_after_health_loss(self):
        adb=ADB.__new__(ADB); adb.guard=Mock(); adb.guard.require.side_effect=RuntimeHealthError('lost')
        adb.ld_index=0
        with patch('gem_bot.ld_tap') as tap,patch('gem_bot.ld_swipe') as swipe,patch('ldplayer_backend.ld_back') as back:
            for call in (lambda:adb.tap(1,2),lambda:adb.swipe(1,2,3,4),adb.back,adb.screenshot):
                with self.assertRaises(RuntimeHealthError): call()
            tap.assert_not_called();swipe.assert_not_called();back.assert_not_called()


class BotTests(unittest.TestCase):
    def bot(self):
        b=GemBot.__new__(GemBot)
        b.cfg={};b.adb=Mock();b.fast_ui=Mock();b.fast_ui.available=True
        b.metrics=SessionMetrics();b._gem_dispatch_owned=True;b._dispatch_pending=True;b._rescue_after=0
        b.last_failure_code='';b.actions=0;b.marches_sent=0;b.goals_done=0
        b.status=Mock();b._finder_failures=0;b.selector_attempt=0
        return b

    def test_march_capacity_5_6_7(self):
        for total in (5,6,7):
            for used in range(total+1):
                self.assertEqual(GemBot._parse_march_counter_text(f'{used}/{total}'),(used,total))

    def test_press_hold_and_castle_durations(self):
        b=self.bot();b._press_xy(100,200);b.adb.swipe.assert_called_with(100,200,100,200,180)
        b._long_hold(20,30,milliseconds=900,label='Castle');b.adb.swipe.assert_called_with(20,30,20,30,900)
        b._long_hold(20,30,milliseconds=750,label='Tree');b.adb.swipe.assert_called_with(20,30,20,30,750)

    def test_dispatch_requires_two_map_frames(self):
        b=self.bot();b._best_template_hit=Mock(side_effect=[None,('tree',{}),None,None,('tree',{}),None])
        with patch('gem_bot.time.sleep'):
            self.assertTrue(b._confirm_dispatch())
        self.assertFalse(b._dispatch_pending);self.assertEqual(b.metrics.counters['dispatch_confirmations'],1)

    def test_unchanged_march_is_not_success(self):
        b=self.bot();b._best_template_hit=Mock(return_value=('march',{}))
        with patch('gem_bot.time.sleep'):
            self.assertFalse(b._confirm_dispatch(timeout=0.005))
        self.assertEqual(b.metrics.counters['dispatch_confirmations'],0)
        self.assertTrue(b._dispatch_pending)

    def test_blank_transition_is_not_success(self):
        b=self.bot();b._best_template_hit=Mock(return_value=None)
        with patch('gem_bot.time.sleep'):
            self.assertFalse(b._confirm_dispatch(timeout=0.005))

    def test_rescue_requires_bot_owned_dispatch(self):
        b=self.bot();b._dispatch_pending=False;b._gem_dispatch_owned=False
        self.assertFalse(b._rescue_ready_march_screen());b.adb.screenshot.assert_not_called()

    def test_ambiguous_previous_press_cannot_be_rescued_twice(self):
        b=self.bot();b._dispatch_pending=True
        self.assertFalse(b._rescue_ready_march_screen());b.adb.screenshot.assert_not_called()

    def test_rescue_cooldown_blocks_duplicate_press(self):
        b=self.bot();b._rescue_after=time.monotonic()+10
        self.assertFalse(b._rescue_ready_march_screen());b.adb.screenshot.assert_not_called()

    def test_rescue_returns_once_without_new_search(self):
        b=self.bot();b._rescue_ready_march_screen=Mock(return_value=True);b.gather_one_fast=Mock()
        self.assertTrue(b.gather_one());self.assertEqual(b.marches_sent,1);b.gather_one_fast.assert_not_called()

    def test_disabled_ai_never_loads_model(self):
        b=self.bot();b.cfg={'ai_fallback':False};b._vision=Mock()
        self.assertFalse(b.perform_goal('test','goal'));b._vision.assert_not_called()

    def test_failed_finder_tier_rotates(self):
        b=self.bot();icons=[{'cx':10},{'cx':30},{'cx':50}]
        self.assertEqual(b._choose_gem_selector_icon(icons)[0],2)
        b._finder_failures=1;self.assertEqual(b._choose_gem_selector_icon(icons)[0],1)

    def test_finder_circuit_does_not_press(self):
        b=self.bot();b._finder_after=time.monotonic()+30
        self.assertFalse(b.prepare_builtin_gem_search());b.adb.screenshot.assert_not_called()

    def test_state_heartbeat_does_not_extend_deadline(self):
        clock=Mock(return_value=0);w=StateWatchdog(clock);w.enter('march')
        clock.return_value=40;w.enter('march');self.assertTrue(w.snapshot()['watchdog_overdue'])
        w.enter('waiting_for_marches');self.assertFalse(w.snapshot()['watchdog_overdue'])

    def test_metric_percentiles_are_bounded(self):
        m=SessionMetrics()
        for n in range(200):m.timing('dispatch',n)
        self.assertEqual(len(m.latencies['dispatch']),120)
        self.assertGreater(m.snapshot()['dispatch_p95_seconds'],m.snapshot()['dispatch_p50_seconds'])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.client=web.app.test_client();self.headers={'X-Account-Token':web.ACCOUNT_TOKEN}
    def test_dashboard_has_companion_and_account_controls(self):
        r=self.client.get('/');self.assertEqual(r.status_code,200)
        for label in [b'Companion control',b'loginCredentials',b'10s refresh',b'3.5.1']:
            self.assertIn(label,r.data)
    def test_start_without_pass_never_launches_emulator(self):
        with patch.object(web,'bot_pid',return_value=0),patch.object(web,'ensure_headless') as emulator:
            r=self.client.post('/api/bot/start',headers=self.headers)
        self.assertEqual(r.status_code,409);emulator.assert_not_called()
    def test_runtime_gate_requires_token(self):
        self.assertEqual(self.client.get('/api/runtime/gate').status_code,403)
        r=self.client.get('/api/runtime/gate',headers={'X-GemOps-Gate':web.GATE_TOKEN})
        self.assertEqual(r.status_code,200);self.assertFalse(r.json['passed'])
    def test_control_requests_require_ui_token(self):
        self.assertEqual(self.client.post('/api/frida/start').status_code,403)
    def test_four_minute_config_cannot_be_extended(self):
        r=self.client.post('/api/config',headers=self.headers,json={'gem_search_timeout_seconds':999,'marches_to_send':7})
        self.assertEqual(r.json['config']['gem_search_timeout_seconds'],240)
        self.assertTrue(r.json['config']['auto_fill_all_marches'])
    def test_invalid_config_is_client_error(self):
        r=self.client.post('/api/config',headers=self.headers,json={'slot':'bad'})
        self.assertEqual(r.status_code,400)
    def test_stop_companion_stops_bot_first(self):
        calls=[]
        with patch.object(web,'stop_bot',side_effect=lambda:calls.append('bot')),patch.object(web.COMPANION,'stop',side_effect=lambda:calls.append('companion')):
            r=self.client.post('/api/frida/stop',headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(calls,['bot','companion'])
    def test_status_read_is_bounded(self):
        web.LOG_PATH.write_text('x'*200000+'\n@@KS_STATUS@@'+json.dumps({'state':'running','task':'find_gem'})+'\n')
        self.assertLessEqual(len(web.read_recent_log()),131072)
        with patch.object(web,'bot_pid',return_value=0):
            self.assertEqual(self.client.get('/api/status').json['state'],'stopped')


if __name__ == '__main__':
    unittest.main(verbosity=2)
