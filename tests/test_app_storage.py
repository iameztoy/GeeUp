"""Storage selection must preserve history and never write on read-only refresh."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_storage as storage
import ee_replacement_register as registry


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        legacy = patch.object(storage, "LEGACY_CONSOLIDATION_ROOT", self.root / "legacy")
        legacy.start()
        self.addCleanup(legacy.stop)

    def test_new_windows_installation_uses_user_data_without_writes(self):
        with patch.object(storage.sys, "platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            expected = self.root / "SWOTFlow" / "ee_consolidation"
            self.assertEqual(storage.consolidation_root(), expected)
            self.assertEqual(registry.default_path("projects/example/assets/source").parent, expected / "registers")
            self.assertFalse(expected.exists())

    def test_legacy_history_is_not_silently_abandoned(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        evidence = legacy / "plan.json"
        evidence.write_text('{"history":"retained"}', encoding="utf-8")
        self.assertEqual(storage.consolidation_root(), legacy)
        self.assertEqual(evidence.read_text(encoding="utf-8"), '{"history":"retained"}')

    def test_explicit_override_wins_without_merging_or_creating_folders(self):
        (self.root / "legacy").mkdir()
        custom = self.root / "custom"
        with patch.dict(os.environ, {"SWOTFLOW_DATA_HOME": str(custom)}):
            self.assertEqual(storage.consolidation_root(), custom / "ee_consolidation")
            self.assertFalse(custom.exists())

    def test_relative_override_rejected_and_linux_location_supported(self):
        with patch.dict(os.environ, {"SWOTFLOW_DATA_HOME": "relative"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                storage.consolidation_root()
        with patch.object(storage.sys, "platform", "linux"), patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}):
            self.assertEqual(storage.data_home(), self.root / "SWOTFlow")
