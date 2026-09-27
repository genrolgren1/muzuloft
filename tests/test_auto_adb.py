import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import ldplayer_backend as ld


class AutoAdbTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.path=self.home/'vms/config/leidian3.config'
        self.path.parent.mkdir(parents=True)
        self.data={'basicSettings.adbDebug':0,'basicSettings.rootMode':False,'unrelated':{'keep':'yes'}}
        self.path.write_text(json.dumps(self.data),encoding='utf-8')
        p=patch.object(ld,'ldplayer_home',return_value=self.home);p.start();self.addCleanup(p.stop)

    def test_enables_local_and_preserves_other_fields_with_backup(self):
        original=self.path.read_bytes()
        with patch.object(ld,'instance',return_value={'pid':'-1'}):self.assertTrue(ld.ensure_local_adb_enabled(3))
        actual=json.loads(self.path.read_text());self.assertEqual(actual.pop('basicSettings.adbDebug'),1)
        expected=dict(self.data);expected.pop('basicSettings.adbDebug');self.assertEqual(actual,expected)
        backups=list(self.path.parent.glob('*.bak'));self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),original)

    def test_already_enabled_does_not_restart_or_rewrite(self):
        self.data['basicSettings.adbDebug']=1;self.path.write_text(json.dumps(self.data))
        original=self.path.read_bytes()
        with patch.object(ld,'stop_instance') as stop,patch.object(ld,'instance') as query:
            self.assertFalse(ld.ensure_local_adb_enabled(3))
        stop.assert_not_called();query.assert_not_called();self.assertEqual(self.path.read_bytes(),original)

    def test_running_selected_instance_stops_before_write(self):
        calls=[]
        with patch.object(ld,'instance',side_effect=[{'pid':'123'},{'pid':'-1'}]),patch.object(ld,'stop_instance',side_effect=lambda idx:calls.append(('stop',idx)) or True),patch.object(ld,'_write_local_adb_config',side_effect=lambda p:calls.append(('write',p)) or True):
            ld.ensure_local_adb_enabled(3)
        self.assertEqual(calls,[('stop',3),('write',self.path)])

    def test_shutdown_saved_changes_are_preserved(self):
        def stop(idx):
            self.data['unrelated']['new']='saved on shutdown';self.path.write_text(json.dumps(self.data));return True
        with patch.object(ld,'instance',side_effect=[{'pid':'123'},{'pid':'-1'}]),patch.object(ld,'stop_instance',side_effect=stop):
            ld.ensure_local_adb_enabled(3)
        self.assertEqual(json.loads(self.path.read_text())['unrelated']['new'],'saved on shutdown')

    def test_failed_stop_cannot_edit_live_configuration(self):
        original=self.path.read_bytes()
        with patch.object(ld,'instance',return_value={'pid':'123'}),patch.object(ld,'stop_instance',return_value=False):
            with self.assertRaises(RuntimeError):ld.ensure_local_adb_enabled(3)
        self.assertEqual(self.path.read_bytes(),original)

    def test_unknown_format_is_unchanged(self):
        self.path.write_text('{"basicSettings.adbDebug":"unknown"}')
        with patch.object(ld,'stop_instance') as stop:
            with self.assertRaises(RuntimeError):ld.ensure_local_adb_enabled(3)
        stop.assert_not_called();self.assertIn('unknown',self.path.read_text())

    def test_missing_file_never_creates_empty_emulator_config(self):
        with self.assertRaises(RuntimeError):ld.ensure_local_adb_enabled(4)
        self.assertFalse((self.path.parent/'leidian4.config').exists())

    def test_negative_index_is_not_silently_main(self):
        with self.assertRaises(ValueError):ld.ensure_local_adb_enabled(-1)

    def test_remote_setting_becomes_local(self):
        self.data['basicSettings.adbDebug']=2;self.path.write_text(json.dumps(self.data))
        with patch.object(ld,'instance',return_value={'pid':'-1'}):ld.ensure_local_adb_enabled(3)
        self.assertEqual(json.loads(self.path.read_text())['basicSettings.adbDebug'],1)

    def test_launch_enables_adb_before_starting(self):
        calls=[]
        with patch.object(ld,'ensure_instance_exists'),patch.object(ld,'ensure_local_adb_enabled',side_effect=lambda idx:calls.append('adb')),patch.object(ld,'is_running',return_value=False),patch.object(ld,'ldconsole',side_effect=lambda *a,**kw:calls.append(a[0]) or Mock(returncode=0)):
            ld.start_instance(3)
        self.assertEqual(calls,['adb','launch'])


if __name__=='__main__':unittest.main(verbosity=2)
