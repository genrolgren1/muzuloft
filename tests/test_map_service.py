import io
import unittest
from unittest.mock import Mock, patch
from PIL import Image
import test_reliability as base
from companion_runtime import CompanionRuntime
from fast_gem_ui import FastGemUI
from gem_bot import GemBot
from pathlib import Path


class MapServiceTests(unittest.TestCase):
    def test_map_endpoint_needs_authentication(self):
        self.assertEqual(base.web.app.test_client().post('/api/runtime/map',data=b'image').status_code,403)

    def test_map_endpoint_requires_live_companion(self):
        with patch.object(base.web.COMPANION,'require',side_effect=RuntimeError('no health')):
            response=base.web.app.test_client().post('/api/runtime/map',data=b'image',headers={'X-GemOps-Gate':base.web.GATE_TOKEN})
        self.assertEqual(response.status_code,503)

    def test_map_endpoint_rejects_oversized_image(self):
        response=base.web.app.test_client().post('/api/runtime/map',data=b'x'*3_000_001,headers={'X-GemOps-Gate':base.web.GATE_TOKEN})
        self.assertEqual(response.status_code,413)

    def test_no_scan_without_live_health(self):
        c=CompanionRuntime(Path(__file__).resolve().parents[1])
        with self.assertRaises(RuntimeError):c.analyze_map(b'image')

    def test_rpc_timeout_is_bounded_and_clears_waiter(self):
        c=CompanionRuntime(Path(__file__).resolve().parents[1]);c.require=Mock();c.process=Mock()
        out=io.BytesIO();Image.new('RGB',(16,16)).save(out,'PNG')
        with self.assertRaises(TimeoutError):c.analyze_map(out.getvalue(),timeout=.01)
        self.assertEqual(c.map_pending,{})
        self.assertTrue(c.map_lock.acquire(blocking=False));c.map_lock.release()

    def test_stop_wakes_pending_scanner(self):
        import threading
        c=CompanionRuntime(Path(__file__).resolve().parents[1]);waiting={'event':threading.Event()};c.map_pending['id']=waiting
        c.stop();self.assertTrue(waiting['event'].is_set());self.assertIn('error',waiting['reply'])

    def test_frida_regions_need_local_validation(self):
        b=base.BotTests().bot();b.adb.guard.analyze_map.return_value={'regions':[[.2,.2,.5,.5]]}
        b._preferred_gem_scales=Mock(return_value=())
        b.fast_ui.find_all.side_effect=[[{'score':.9}], [{'score':.7,'full':True}]]
        b.fast_ui.classify_gem_candidate.return_value={'verdict':'reject'}
        self.assertTrue(b._scan_gem_candidates_impl(b'image',threshold=.57,max_results=12)[0]['full'])

    def test_good_frida_region_avoids_full_frame_scan(self):
        b=base.BotTests().bot();b.adb.guard.analyze_map.return_value={'regions':[[.2,.2,.5,.5]]}
        b.fast_ui.find_all.return_value=[{'score':.9}]
        b.fast_ui.classify_gem_candidate.return_value={'verdict':'accept'}
        self.assertEqual(len(b._scan_gem_candidates_impl(b'image',threshold=.57,max_results=12)),1)
        b.fast_ui.find_all.assert_called_once()
        self.assertEqual(b.fast_ui.find_all.call_args.kwargs['region'],(.2,.2,.5,.5))

    def test_current_world_control_confirms_map_return(self):
        b=base.BotTests().bot();b.fast_ui=FastGemUI()
        im=Image.new('RGB',(1280,720),(140,155,80))
        im.paste(Image.open(Path(__file__).resolve().parents[1]/'gem_templates/world_search_button.png'),(22,509))
        out=io.BytesIO();im.save(out,'PNG');b.adb.screenshot.return_value=out.getvalue()
        with patch('gem_bot.time.sleep'):self.assertTrue(b._confirm_dispatch())
        self.assertFalse(b._dispatch_pending)

    def test_ready_march_panel_cannot_count_as_sent(self):
        b=base.BotTests().bot();b._best_template_hit=Mock(side_effect=lambda png,names,**kw:None if names[0]=='new_troop_screen_title' else ('hit',{}))
        with patch('gem_bot.time.sleep'):self.assertFalse(b._confirm_dispatch(timeout=.005))
        self.assertTrue(b._dispatch_pending)

    def test_owned_new_troop_is_resumed_without_search(self):
        b=base.BotTests().bot();b._dispatch_pending=False
        b._rescue_ready_march_screen=Mock(return_value=False)
        b._best_template_hit=Mock(return_value=('new_troop_button_v2',{}))
        b.dispatch_selected_gem_fast=Mock(return_value=True);b.gather_one_fast=Mock()
        self.assertTrue(b.gather_one())
        b.dispatch_selected_gem_fast.assert_called_once_with(resume=True)
        b.gather_one_fast.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
