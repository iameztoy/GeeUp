"""Personal upload defaults never migrate existing projects or retain secrets."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from swotflow.project.model import create_project, load_project, save_project
from swotflow.project.upload_defaults import load_upload_defaults, save_upload_defaults


class UploadDefaultsTests(unittest.TestCase):
    def settings(self):
        return {'destination_parent':'projects/example-project/assets/collection',
                'cloud_upload':{'project':'example-project','bucket':'sample-bucket','concurrent_uploads':2},
                'upload':{'backend':'gcs','completion_mode':'wait_for_ee','max_active_ingestions':2,
                          'pyramiding_policy':{'default':'SAMPLE','per_band':{}}},
                'execution':{'dry_run':False}}

    def test_missing_personal_preset_retains_browser_default(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(load_upload_defaults(Path(t)/'absent.json'))
            p=create_project(Path(t)/'new','New',self.settings())
            self.assertEqual(p.config['upload']['backend'],'browser')

    def test_personal_preset_applies_after_recommended_defaults_but_preserves_paths_and_scope(self):
        with tempfile.TemporaryDirectory() as t:
            prefs=Path(t)/'preferences.json'; choice=self.settings()
            choice.update(download={'start_date':'2023-01-01','utm_tiles':['UTM35L']},credentials={'password':'do-not-save'})
            save_upload_defaults(choice,prefs)
            self.assertNotIn('password',prefs.read_text(encoding='utf-8'))
            self.assertNotIn('2023-01-01',prefs.read_text(encoding='utf-8'))
            base={'download':{'start_date':'2024-01-01','end_date':'2024-12-31','utm_tiles':['UTM36P']},'gdal':{'python':'my-runtime'}}
            original=deepcopy(base)
            p=create_project(Path(t)/'new','Cloud',base,upload_defaults=load_upload_defaults(prefs))
            self.assertEqual(p.config['upload']['backend'],'gcs')
            self.assertEqual(p.config['cloud_upload']['concurrent_uploads'],2)
            self.assertEqual(p.config['upload']['completion_mode'],'wait_for_ee')
            self.assertFalse(p.config['execution']['dry_run'])
            self.assertEqual(p.config['download']['utm_tiles'],['UTM36P'])
            self.assertEqual(p.config['gdal'],base['gdal'])
            self.assertEqual(p.config['input_folder'],str(p.root/'03_mosaics'))
            self.assertTrue(p.config['download']['verified_downloads'])
            self.assertTrue(p.config['automation']['process_by_year'])
            self.assertEqual(base,original)
            self.assertEqual(load_project(p.root).config,p.config)

    def test_open_keeps_existing_choices_history_and_user_edits(self):
        with tempfile.TemporaryDirectory() as t:
            p=create_project(Path(t)/'old','Old',{})
            p.download_history=[{'downloaded_count':12}];save_project(p)
            before=p.project_file.read_bytes()
            save_upload_defaults(self.settings(),Path(t)/'defaults.json')
            loaded=load_project(p.root)
            self.assertEqual(loaded.config['upload']['backend'],'browser')
            self.assertEqual(loaded.download_history,p.download_history)
            self.assertEqual(p.project_file.read_bytes(),before)
            new=create_project(Path(t)/'new','New',{},upload_defaults=self.settings())
            new.config['cloud_upload']['concurrent_uploads']=8;save_project(new)
            self.assertEqual(load_project(new.root).config['cloud_upload']['concurrent_uploads'],8)

    def test_invalid_preset_does_not_silently_fallback_or_create_project(self):
        with tempfile.TemporaryDirectory() as t:
            prefs=Path(t)/'defaults.json';prefs.write_text('{broken',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Save them again'):
                load_upload_defaults(prefs)
            choice=self.settings();choice['cloud_upload']['concurrent_uploads']=9
            with self.assertRaises(ValueError):
                create_project(Path(t)/'new','New',{},upload_defaults=choice)
            self.assertFalse((Path(t)/'new').exists())

    def test_invalid_save_keeps_previous_preset(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'defaults.json';save_upload_defaults(self.settings(),p);before=p.read_bytes()
            choice=self.settings();choice['destination_parent']=''
            with self.assertRaises(ValueError):save_upload_defaults(choice,p)
            self.assertEqual(p.read_bytes(),before)

    def test_gui_new_project_uses_saved_preset(self):
        from swotflow.ui.main import LauncherApp
        with tempfile.TemporaryDirectory() as t:
            app=SimpleNamespace(automation_running=False,automation_preflight_running=False,root=object(),
                                build_config=lambda:{},apply_project=mock.Mock())
            with mock.patch('swotflow.ui.main.filedialog.askdirectory',return_value=str(Path(t)/'new')), \
                 mock.patch('swotflow.ui.main.simpledialog.askstring',return_value='New'), \
                 mock.patch('swotflow.ui.main.messagebox.showinfo'), \
                 mock.patch('swotflow.project.upload_defaults.load_upload_defaults',return_value=self.settings()):
                LauncherApp.new_project(app)
            project=app.apply_project.call_args.args[0]
            self.assertEqual(project.config['cloud_upload']['concurrent_uploads'],2)
            self.assertEqual(project.config['upload']['backend'],'gcs')

    def test_saving_from_cloud_tab_does_not_save_current_project(self):
        from swotflow.earthengine.cloud.tab import CloudUploadTab
        app=SimpleNamespace(build_config=lambda:self.settings(),save_project=mock.Mock())
        tab=SimpleNamespace(app=app)
        with tempfile.TemporaryDirectory() as t, \
             mock.patch('swotflow.project.upload_defaults.defaults_path',return_value=Path(t)/'defaults.json'), \
             mock.patch('swotflow.earthengine.cloud.tab.messagebox.showinfo'):
            CloudUploadTab.save_defaults(tab)
            self.assertEqual(load_upload_defaults(Path(t)/'defaults.json')['upload']['backend'],'gcs')
        app.save_project.assert_not_called()
