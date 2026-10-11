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

    def test_runtime_does_not_reintroduce_private_workflow(self):
        source = Path(__file__).parent
        forbidden = ('Source GUID', 'SourceGUID', 'NOT VALID', 'ItemThickness',
                     'prepare_pdf_colors', 'PDF_LINK_PALETTE', 'pull_plugin_icons',
                     'C:\\Shared', 'C:\\Users')
        for name in install.RUNTIME_FILES:
            text = (source / name).read_text(encoding='utf-8-sig')
            for term in forbidden:
                with self.subTest(file=name, term=term):
                    self.assertNotIn(term, text)

    def test_bridge_page_items_are_independent_of_takeoff(self):
        source = Path(__file__).parent
        script = source / 'Takeoff PDF Community - PlanSwift Script.txt'
        if not script.exists():
            script = source.parent / script.name
        script = Path(os.environ.get('COMMUNITY_SCRIPT', str(script)))
        if not script.exists():
            self.skipTest('Separate bridge TXT not provided; set COMMUNITY_SCRIPT for this audit.')
        text = script.read_text(encoding='utf-8-sig')
        self.assertIn(picker.RESULT_HEADER, text)
        self.assertIn("responseLines.Strings[8] <> 'NONE'", text)
        self.assertIn("responseLines.Strings[9] <> 'PAGES'", text)
        self.assertIn('responseIndex := 10 to marksIndex - 1', text)
        drawing = text.split('procedure WriteDrawingRecord(', 1)[1].split('function ChooseExportFile', 1)[0]
        annotation_block, takeoff_block = drawing.split('  if not ExportDrawingOnly then begin', 1)
        self.assertIn('if ExportPageItems then begin', annotation_block)
        for emit in ('EmitDimensions', 'EmitHighlighters', 'EmitNotes', 'EmitPageAnnotations'):
            self.assertIn(emit + '(drawingId)', annotation_block)
            self.assertNotIn(emit + '(drawingId)', takeoff_block)
        self.assertIn('WriteTraceRecord', takeoff_block)

    def test_section_allocation_annotations_and_defaults_work_together(self):
        import contextlib
        import io
        import fitz
        from test_pdf_source_background import fixture
        from test_pdf_page_annotations import xml
        profile = {'Landscape': dict(rect=[.04,.7,.7,.95], font=12, anchor='bottom')}
        with tempfile.TemporaryDirectory() as temp:
            for background in ('Raster', 'PDF'):
                for mode, count in (('VISIBLE', 1), ('ALL', 2), ('NONE', 0)):
                    manifest, output, _ = fixture(Path(temp) / (background + mode), mode=background)
                    text = 'ItemLayout: SectionRecordsV1\nPageItems: ' + mode + '\n' + manifest.read_text()
                    text = text.replace('PageTotal: 500', 'ParentQty: 1000\nSectionShare: 0.5')
                    annotations = []
                    for visible, y in (('True', 150), ('False', 350)):
                        annotations += ['--- ANNOTATION_START ---', 'NativeType: RubberStamp',
                                        'PrimaryLabel: APPROVED', 'Visible: ' + visible,
                                        'DigitizerData:', xml(((100, y), (400, y))),
                                        '--- ANNOTATION_END ---']
                    text = text.replace('--- PAGE_END ---', '\n'.join(annotations) + '\n--- PAGE_END ---')
                    manifest.write_text(text, encoding='utf-8')
                    with contextlib.redirect_stdout(io.StringIO()):
                        details = builder.build(str(manifest), strict=True, legend_defaults=profile)
                    with fitz.open(output) as pdf:
                        self.assertEqual(len(pdf), 1)
                        self.assertIn('500 LN FT', pdf[0].get_text())
                        self.assertEqual(pdf[0].get_text().count('APPROVED'), count)
                        title = pdf[0].search_for('TAKEOFF SUMMARY')[0]
                        self.assertGreater(title.y0, 420)
                        self.assertTrue(any('/SetOCGState' in pdf.xref_object(i)
                                            for i in range(1, pdf.xref_length())))
                    self.assertEqual(len(details['page_keys']), 1)

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
