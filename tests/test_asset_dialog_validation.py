"""Observed asset-ID length validation must reach safe pre-click recovery."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swotflow.earthengine.uploader import RetryableUIError, normalize_dialog_error_messages
from tests import test_uploader_regressions as fixtures

LENGTH_ERROR = ('Each part of the asset ID between slashes must contain at least 1 character\n'
                'and at most 100 characters.')


REQUIRED_ERROR = 'Please provide an asset ID.'


class AssetDialogValidationTests(unittest.TestCase):
    def make_uploader(self, root):
        fixture = fixtures.UploaderDialogRegressionTests()
        uploader = fixture.uploader(fixture.uploader_config(root))
        uploader._current_dialog_asset_trailer = 'collection/valid_name'
        return uploader

    def test_wrapped_length_validation_survives_normalization(self):
        self.assertEqual(normalize_dialog_error_messages([LENGTH_ERROR, LENGTH_ERROR]),
                         LENGTH_ERROR.replace('\n', ' '))
        self.assertEqual(normalize_dialog_error_messages(['testtiles-461713', 'Asset ID', '']), '')

    def test_required_id_validation_survives_normalization(self):
        self.assertEqual(normalize_dialog_error_messages([REQUIRED_ERROR]), REQUIRED_ERROR)

    def test_disabled_length_validation_retypes_once_before_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader = self.make_uploader(Path(tmp))
            with mock.patch.object(uploader, 'current_ui_upload_button_status', side_effect=[
                    {'found': True, 'disabled': True}, {'found': True, 'disabled': False}]), \
                 mock.patch.object(uploader, 'read_dialog_error_message', return_value=normalize_dialog_error_messages([LENGTH_ERROR])), \
                 mock.patch.object(uploader, 'set_current_ui_asset_name') as retype, \
                 mock.patch.object(uploader, 'execute_script') as click, \
                 mock.patch('swotflow.earthengine.uploader.time.sleep'):
                uploader._click_current_ui_upload_button()
                retype.assert_called_once_with('collection/valid_name', force_keyboard=True)
                click.assert_called_once()

    def test_persistent_validation_or_disappeared_button_never_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            for final in ({'found': True, 'disabled': True}, {'found': False, 'disabled': False}):
                with self.subTest(final=final):
                    uploader = self.make_uploader(Path(tmp))
                    with mock.patch.object(uploader, 'current_ui_upload_button_status', side_effect=[
                            {'found': True, 'disabled': True}, final]), \
                         mock.patch.object(uploader, 'read_dialog_error_message', return_value=normalize_dialog_error_messages([LENGTH_ERROR])), \
                         mock.patch.object(uploader, 'set_current_ui_asset_name') as retype, \
                         mock.patch.object(uploader, 'execute_script') as click, \
                         mock.patch('swotflow.earthengine.uploader.time.sleep'):
                        with self.assertRaises(RetryableUIError):
                            uploader._click_current_ui_upload_button()
                        retype.assert_called_once()
                        click.assert_not_called()

    def test_unrelated_validation_does_not_retype_or_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader = self.make_uploader(Path(tmp))
            with mock.patch.object(uploader, 'current_ui_upload_button_status', return_value={'found': True, 'disabled': True}), \
                 mock.patch.object(uploader, 'read_dialog_error_message', return_value='Invalid file'), \
                 mock.patch.object(uploader, 'set_current_ui_asset_name') as retype, \
                 mock.patch.object(uploader, 'execute_script') as click:
                with self.assertRaisesRegex(RetryableUIError, 'Invalid file'):
                    uploader._click_current_ui_upload_button()
                retype.assert_not_called()
                click.assert_not_called()


@unittest.skipUnless(os.environ.get('SWOTFLOW_BROWSER_TESTS') == '1', 'Opt-in isolated Chrome DOM test')
class AssetDialogDOMTests(unittest.TestCase):
    def test_visible_length_error_triggers_real_keyboard_recovery(self):
        self.check_message(LENGTH_ERROR)

    def test_visible_required_id_triggers_real_keyboard_recovery(self):
        self.check_message(REQUIRED_ERROR)

    def check_message(self, message):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        with tempfile.TemporaryDirectory() as tmp:
            options = Options()
            options.add_argument('--headless=new')
            options.add_argument('--user-data-dir=' + str(Path(tmp) / 'chrome'))
            driver = webdriver.Chrome(options=options)
            try:
                driver.get('data:text/html,<ee-upload-dialog></ee-upload-dialog>')
                driver.execute_script('''
                    const root=document.querySelector('ee-upload-dialog').attachShadow({mode:'open'});
                    root.innerHTML='<ee-dialog id="asset-upload-dialog"></ee-dialog><ee-input id="asset-id-trailer"></ee-input>';
                    const inner=root.querySelector('ee-dialog').attachShadow({mode:'open'});
                    inner.innerHTML='<div role="dialog" style="width:500px;height:100px">Upload a new image</div><button class="ok-button" disabled>Upload</button>';
                    const host=root.querySelector('ee-input').attachShadow({mode:'open'});
                    host.innerHTML='<paper-input id="paper-input"></paper-input><paper-input-error style="display:block"></paper-input-error>';
                    const field=host.querySelector('paper-input').attachShadow({mode:'open'});
                    field.innerHTML='<input value="collection/stale_long_name">';
                    const error=host.querySelector('paper-input-error');error.textContent=arguments[0];
                    const button=inner.querySelector('button');window.clicked=0;button.onclick=()=>window.clicked++;
                    field.querySelector('input').addEventListener('input',event=>{
                      if(event.isTrusted && event.target.value==='collection/valid_name'){
                        button.disabled=false;error.textContent='';
                      }
                    });
                ''', message)
                uploader = AssetDialogValidationTests().make_uploader(Path(tmp))
                uploader.driver = driver
                self.assertEqual(uploader.read_dialog_error_message(), ' '.join(message.split()))
                uploader._click_current_ui_upload_button()
                self.assertEqual(driver.execute_script('return window.clicked;'), 1)
                self.assertEqual(uploader.read_dialog_error_message(), '')
            finally:
                driver.quit()
