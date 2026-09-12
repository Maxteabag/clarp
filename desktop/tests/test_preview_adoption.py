import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('adoption',Path(__file__).parents[1]/'tools/adopt_preview.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class Adoption(unittest.TestCase):
    def test_copy_preserves_source_and_rejects_change(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'old.conf';source.write_bytes(b'[draft]\ntext=unsent\nattachments=preserved\n');before=source.read_bytes()
            a.copy_settings(source,p/'copy.conf')
            self.assertEqual(source.read_bytes(),before);self.assertEqual((p/'copy.conf').read_bytes(),before)
            with patch.object(a.time,'sleep',side_effect=lambda _:source.write_bytes(b'new draft')):
                with self.assertRaisesRegex(ValueError,'changed'):a.copy_settings(source,p/'rejected.conf')
            self.assertFalse((p/'rejected.conf').exists())

    def test_one_shot_context_and_private_settings(self):
        r={'config_home':'/private/config','host':'http://fixture','session':'exact-session'}
        env=a.launch_environment(r,{'CLARP_SCREENSHOT_PATH':'old.png','CLARP_RESTORE_SESSION':'wrong','UNRELATED':'keep','XDG_ACTIVATION_TOKEN':'stale','DESKTOP_STARTUP_ID':'stale','CLARP_INSTANCE_NAME':'old'})
        self.assertEqual(env['CLARP_RESTORE_SESSION'],'exact-session');self.assertEqual(env['CLARP_RESTORE_DESKTOP'],'1')
        self.assertEqual(env['XDG_CONFIG_HOME'],'/private/config');self.assertNotIn('CLARP_SCREENSHOT_PATH',env)
        self.assertEqual(env['UNRELATED'],'keep')
        for key in ('XDG_ACTIVATION_TOKEN','DESKTOP_STARTUP_ID','CLARP_INSTANCE_NAME'):self.assertNotIn(key,env)

    def test_source_title_requires_unique_host_session(self):
        import io,json
        with tempfile.TemporaryDirectory() as d:
            config=Path(d);(config/'MaxTeaBag').mkdir();(config/'MaxTeaBag'/'Clarp.conf').write_text('[connection]\nbaseUrl=http://fixture\n')
            env={'CLARP_TOKEN':'fixture-only'}
            def response(agents):return io.BytesIO(json.dumps({'agents':agents}).encode())
            exact={'session':'exact','persona':'Unique','agent_id':'id'}
            with patch.object(a,'process_environment',return_value=env),patch.object(a.urllib.request,'urlopen',return_value=response([exact])):
                self.assertEqual(a.verify_session(42,'exact','http://fixture','Unique — Clarp',config,'Clarp')['session'],'exact')
            for agents,session in [([exact],'wrong'),([exact,exact|{'session':'duplicate'}],'exact')]:
                with patch.object(a,'process_environment',return_value=env),patch.object(a.urllib.request,'urlopen',return_value=response(agents)):
                    with self.assertRaisesRegex(ValueError,'uniquely'):a.verify_session(42,session,'http://fixture','Unique — Clarp',config,'Clarp')
            with patch.object(a,'process_environment',return_value=env):
                with self.assertRaisesRegex(ValueError,'Host'):a.verify_session(42,'exact','http://wrong','Unique — Clarp',config,'Clarp')

    def test_changed_process_or_draft_rejects_adoption(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'old.conf';source.write_text('draft');target=p/'MaxTeaBag'/'Clarp.conf';target.parent.mkdir();target.write_text('draft');binary=p/'binary';binary.write_bytes(b'fixture')
            identity={'pid':42,'start_time':'1','binary_sha256':'old'}
            r={'verified_identity':{},'session':'s','host':'http://fixture','source':identity,'source_title':'Fixture','source_address':'addr','workspace':5,'source_settings':str(source),'settings_sha256':a.digest(source),'config_home':d,'namespace':'Clarp','binary':str(binary),'binary_sha256':a.digest(binary)}
            with patch.object(a,'verify_session',return_value={}),patch.object(a,'process_identity',return_value=identity),patch.object(a,'source_window',return_value={'address':'addr','workspace':{'id':5}}):
                a.validate(r);source.write_text('new draft')
                with self.assertRaisesRegex(ValueError,'Draft/settings changed'):a.validate(r)
            with patch.object(a,'process_identity',return_value=identity|{'start_time':'2'}):
                with self.assertRaisesRegex(ValueError,'Source process changed'):a.validate(r)

if __name__=='__main__':unittest.main()
