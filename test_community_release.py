# SPDX-License-Identifier: AGPL-3.0-only
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pdf_takeoff_builder as builder
import pdf_export_picker as picker
import install


class CommunityTests(unittest.TestCase):
    def test_colors_are_not_reinterpreted_by_private_metadata(self):
        item = {'Color': '0', 'SourceGUID': 'example', 'PDFColor': '#00FF00', '_PDFColor': '255'}
        self.assertEqual(builder.pdf_item_color(item), '0')
        self.assertEqual(builder.pdf_item_color({'Color': '255'}), '255')

    def test_default_paths_are_separate_and_per_user(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': 'C:/ExampleUser/Local'}):
            root = install.installation_root()
            self.assertEqual(root.name, 'Takeoff PDF Community')
            self.assertEqual(picker.default_preferences_path().parent, root)

    def test_installer_copies_only_runtime_and_keeps_preferences(self):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'installed'
            target.mkdir()
            settings = target / 'preferences.json'
            settings.write_text('{"preview_view":"List"}')
            with patch.object(install.subprocess, 'run') as run:
                python = install.install(source, target)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(settings.read_text(), '{"preview_view":"List"}')
            self.assertEqual(python, target / 'runtime' / 'Scripts' / 'python.exe')
            for name in install.RUNTIME_FILES:
                self.assertEqual((source / name).read_bytes(), (target / name).read_bytes())
            self.assertTrue((target / 'assets' / 'area.png').exists())
            self.assertFalse(list(target.glob('test_*.py')))

    def test_separate_bridge_has_no_private_filters_or_paths(self):
        default = Path(__file__).parent / 'Takeoff PDF Community - PlanSwift Script.txt'
        if not default.exists():
            default = Path(__file__).parent.parent / default.name
        script = Path(os.environ.get('COMMUNITY_SCRIPT', str(default)))
        if not script.exists():
            self.skipTest('Separate bridge TXT not provided; set COMMUNITY_SCRIPT for this audit.')
        text = script.read_text(encoding='utf-8-sig')
        for private in ('Source GUID', 'SourceGUID', 'NOT VALID', "'Building'", 'EmitPDFColorMetadata',
                        'linkedLst', 'pull_plugin_icons', 'C:\\Shared', 'C:\\Users'):
            self.assertNotIn(private, text)
        self.assertIn('ItemLayout: SectionRecordsV1', text)
        self.assertIn("GetResultAsFloat(parentPath, 'Qty', 0)", text)
        self.assertIn("GetResultAsFloat(tracePath, 'Takeoff %', 0)", text)
        for inherited in ('getChilditems', 'GetChildPages', 'itmLst', 'sectionLst',
                          'ExportType', 'NewForm', 'Clipboard.AsText', 'Excel.Application'):
            self.assertNotIn(inherited.lower(), text.lower())
        self.assertIn("getProperty('Qty').inputUnits", text)
        self.assertIn("ExpandEnvironmentStrings('%LOCALAPPDATA%')", text)
        self.assertLess(text.index("  installRoot :="), text.index('  if (GetAsyncKeyState(17)'))


if __name__ == '__main__':
    unittest.main()
