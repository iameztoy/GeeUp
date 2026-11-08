"""A lost response retains the unique ingestion revision for later EE evidence."""
from contextlib import ExitStack
import unittest
from unittest import mock

from selenium.common.exceptions import WebDriverException
from swotflow.earthengine.uploader import UNKNOWN_AFTER_CLICK_STATUS, EE_VERIFIED_EXISTS_STATUS
from swotflow.verification import pipeline as p
from tests import test_pipeline_verification as fixtures
from tests import test_automatic_upload_recovery as uploads


class InterruptedIngestionTests(unittest.TestCase):
    setUp = fixtures.ProofTests.setUp

    def test_attempted_revision_confirms_only_when_exact_cloud_properties_arrive(self):
        receipt = self.store.prepare_ingestion(self.asset['id'], self.mosaic_proof)
        self.asset['properties'] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        self.assertFalse(p.revision_matches(self.store, self.mosaic_proof, self.asset))
        self.store.record_ingestion({**receipt, 'phase': 'attempted'})
        self.asset['properties']['swotflow_revision'] = 'another-attempt'
        p.save_inventory(self.store, self.inventory)
        self.assertFalse(p.revision_matches(self.store, self.mosaic_proof, self.asset))
        self.asset['properties'] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        self.assertTrue(p.revision_matches(self.store, self.mosaic_proof, self.asset))
        self.assertEqual(self.store.latest_asset_event('ingestions', self.asset['id'])['phase'], 'confirmed')

    def test_lost_upload_response_recovers_from_late_revision_without_second_click(self):
        uploader, item = uploads.ActiveTransferTests().setup_browser(self.root / 'browser')
        uploader.config.verified_workflow = True
        uploader.config.mosaic_manifest_csv = self.store.logs / 'mosaic_manifest.csv'
        item.local_file = self.mosaic
        item.asset_id = self.asset['id']
        def lost_response(_item):
            receipt = self.store.latest_asset_event('ingestions', item.asset_id)
            self.assertEqual(receipt['phase'], 'attempted')
            self.assertEqual(receipt['local_sha256'], self.mosaic_proof['sha256'])
            raise WebDriverException('lost response after successful server acceptance')
        with ExitStack() as stack:
            for method in ('ensure_assets_tab', 'open_image_upload_dialog', 'populate_upload_dialog',
                           'remember_browser_upload_baseline', 'close_dialog_if_possible'):
                stack.enter_context(mock.patch.object(uploader, method))
            click = stack.enter_context(mock.patch.object(uploader, 'submit_upload_dialog', side_effect=lost_response))
            uploader.submit_with_retries(item)
        click.assert_called_once()
        self.assertEqual(uploader.report.get_status(item.asset_id), UNKNOWN_AFTER_CLICK_STATUS)
        self.asset['properties'] = p.ingestion_properties(item.ingestion_revision)
        p.save_inventory(self.store, self.inventory)
        uploader._verified_inventory = {item.asset_id: self.asset}
        self.assertEqual(uploader.existing_revision_status(item.local_file, item.asset_id), EE_VERIFIED_EXISTS_STATUS)
        self.assertTrue(self.mosaic.exists())


if __name__ == '__main__':
    unittest.main()
