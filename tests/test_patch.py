"""Regression coverage for the missed-node and exit-dialog report."""
import io
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from PIL import Image
import test_reliability as base
from gem_bot import ADB, GemBot
from fast_gem_ui import FastGemUI
from search_brain import SearchPoint, SearchBrain


def frame(color=(90, 155, 45)):
    out = io.BytesIO()
    Image.new('RGB', (1280, 720), color).save(out, 'PNG')
    return out.getvalue()


class PatchTests(unittest.TestCase):
    def test_back_cannot_emit_an_android_key(self):
        adb=ADB.__new__(ADB); adb.guard=Mock(); adb.ld_index=0
        with patch('ldplayer_backend.ld_back') as send, patch('gem_bot.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Back is disabled'):
                adb.back()
        send.assert_not_called();run.assert_not_called()

    def test_exit_notice_blocks_screen_before_ai_or_search(self):
        im=Image.new('RGB',(1280,720),(0,70,90))
        im.paste(Image.open(Path(__file__).resolve().parents[1]/'gem_templates/exit_game_notice.png'),(540,303))
        out=io.BytesIO();im.save(out,'PNG')
        adb=ADB.__new__(ADB)
        with self.assertRaisesRegex(RuntimeError,'Choose CANCEL'):
            adb._checked_screen(out.getvalue())

    def test_normal_screen_is_not_exit_dialog(self):
        adb=ADB.__new__(ADB);png=frame()
        self.assertEqual(adb._checked_screen(png),png)

    def test_green_terrain_does_not_mean_occupied(self):
        ui=FastGemUI()
        # Same strong gem features; only surrounding green varies.
        features=dict(confidence=.90,template=.90,hist=.90,red_match=.90,edge=.90)
        with patch.object(ui,'gem_candidate_features',return_value=features),patch.object(ui,'occupied_axe_badge_score',return_value=.1):
            for green in (.01,.39,1):
                with patch.object(ui,'green_activity_score',return_value=green):
                    self.assertEqual(ui.classify_gem_candidate(b'',{})['verdict'],'accept')

    def test_axe_still_rejects_occupied_gem(self):
        ui=FastGemUI()
        with patch.object(ui,'gem_candidate_features',return_value={}),patch.object(ui,'occupied_axe_badge_score',return_value=.8),patch.object(ui,'green_activity_score',return_value=0):
            self.assertEqual(ui.classify_gem_candidate(b'',{})['verdict'],'occupied')

    def test_red_prefilter_retains_known_gem(self):
        ui=FastGemUI();im=Image.new('RGB',(1280,720),(90,155,45))
        gem=Image.open(Path(__file__).resolve().parents[1]/'gem_templates/gem_node.png').convert('RGB')
        im.paste(gem,(510,300));out=io.BytesIO();im.save(out,'PNG')
        hits=ui.find_all(out.getvalue(),'gem_node',max_results=5)
        self.assertTrue(any(abs(h['x']-510)<5 and abs(h['y']-300)<5 for h in hits))
        analysis=ui.classify_gem_candidate(out.getvalue(),hits[0])
        self.assertGreater(analysis['green'],.0052)
        self.assertEqual(analysis['verdict'],'accept')
        self.assertEqual(ui.find_all(frame(),'gem_node'),[])

    def test_visible_borderline_is_verified_now_before_finder_or_swipe(self):
        import time
        b=base.BotTests().bot();b._cycle_deadline=time.monotonic()+240
        b.cfg={'borderline_ai_delay_seconds':18,'ai_fallback':True}
        b.search_brain=SearchBrain('smart');b._prune_occupied_gems=Mock()
        b.adb.screenshot.return_value=frame();b._finder_open=False
        hit=dict(cx=640,cy=350,x=600,y=320,w=80,h=60,scale=.5)
        b._scan_gem_candidates=Mock(return_value=[hit])
        b._gem_world_key=Mock(return_value=(0,0));b._recently_rejected_gem=Mock(return_value=False)
        analysis=dict(verdict='borderline',confidence=.7)
        b.fast_ui.classify_gem_candidate.return_value=analysis
        b._temporal_confirm_candidate=Mock(return_value=(True,analysis))
        b._ai_verify_candidate=Mock(return_value=(True,dict(confidence=.99)))
        b._record_candidate=Mock();b.preferred_gem_scale=None
        b._selected_gem_has_gather_button=Mock(return_value=True)
        b.prepare_builtin_gem_search=Mock();b._swipe_search_map=Mock()
        with patch('gem_bot.time.sleep'):
            self.assertTrue(b.find_gem_node_fast())
        b._ai_verify_candidate.assert_called_once()
        b.prepare_builtin_gem_search.assert_not_called();b._swipe_search_map.assert_not_called()
        b.adb.tap.assert_called_once_with(640,350)

    def test_tree_and_castle_matches_are_restricted_to_controls(self):
        b=base.BotTests().bot();b.fast_ui.find.return_value=None
        for name in ('tree_action_reference','leave_city_castle','leave_city_map'):
            b._best_template_hit(b'',(name,),thresholds={name:.62})
            args=b.fast_ui.find.call_args.kwargs
            self.assertEqual(args['region'],(0.,.45,.22,1.))
            self.assertGreaterEqual(args['threshold'],.78)

    def test_existing_world_view_does_not_hold_castle(self):
        b=base.BotTests().bot();b._best_template_hit=Mock(return_value=('tree',{}))
        self.assertTrue(b.ensure_world_map_fast())
        b.adb.swipe.assert_not_called();b.adb.back.assert_not_called()

    def test_unknown_popup_never_sends_back_or_taps(self):
        b=base.BotTests().bot();b._best_template_hit=Mock(return_value=None)
        self.assertFalse(b._dismiss_map_selection())
        b.adb.back.assert_not_called();b.adb.tap.assert_not_called()

    def test_preferred_scale_false_matches_trigger_full_scan(self):
        b=base.BotTests().bot();b._preferred_gem_scales=Mock(return_value=(.5,))
        b.fast_ui.find_all.side_effect=[[{'score':.8}],[{'score':.7,'actual':True}]]
        b.fast_ui.classify_gem_candidate.return_value={'verdict':'reject'}
        self.assertTrue(b._scan_gem_candidates_impl(b'',threshold=.57,max_results=12)[0]['actual'])
        self.assertEqual(b.fast_ui.find_all.call_count,2)

    def test_cycle_searches_view_without_opening_finder_first(self):
        b=base.BotTests().bot();b.ensure_world_map_fast=Mock();b.occupied_gem_targets=[]
        b.prepare_builtin_gem_search=Mock();b.find_gem_node_fast=Mock(return_value=True)
        b.dispatch_selected_gem_fast=Mock(return_value=True)
        self.assertTrue(b.gather_one_fast());b.prepare_builtin_gem_search.assert_not_called()
        b.find_gem_node_fast.assert_called_once();b.dispatch_selected_gem_fast.assert_called_once()

    def test_late_rendered_candidate_cancels_swipe(self):
        import random, time
        b=base.BotTests().bot();b.search_started_monotonic=time.monotonic();b.search_timeout_seconds=240
        b.search_point=SearchPoint();b.search_brain=Mock();b._search_rng=random.Random(1)
        b.adb.screenshot.return_value=frame();b._scan_gem_candidates=Mock(return_value=[{}])
        b._gem_world_key=Mock(return_value=(0,0));b._recently_rejected_gem=Mock(return_value=False)
        b.fast_ui.classify_gem_candidate.return_value={'verdict':'accept'}
        b._swipe_search_map();b.adb.swipe.assert_not_called()
        self.assertEqual(b.metrics.counters['swipes_cancelled_for_candidate'],1)


if __name__=='__main__':
    unittest.main(verbosity=2)
