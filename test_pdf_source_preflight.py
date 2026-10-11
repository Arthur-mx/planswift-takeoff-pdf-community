# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pdf_takeoff_builder as b
from pdf_source_paths import check_source_pages
from test_pdf_source_background import fixture, make_source
from test_pdf_page_labels import make_set

TAKEOFF_PATH = r'..\Pages\Plans & Docs\2026-01-01\Permit set'


def pages(*names):
    return [{'PageName': name, 'TakeoffPath': TAKEOFF_PATH} for name in names]


class SourcePreflightTests(unittest.TestCase):
    def test_checks_all_requested_pages_and_reports_each_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, sources = fixture(root)
            make_source(sources / 'A-102 - first.pdf')
            make_source(sources / 'A-102 - second.pdf')
            (sources / 'A-103.pdf').write_bytes(b'broken PDF')
            with b.fitz.open() as doc:
                doc.new_page()
                doc.save(sources / 'A-104.pdf', encryption=b.fitz.PDF_ENCRYPT_AES_256,
                         owner_pw='owner', user_pw='password')
            names = ('*A-101 | Plan', 'A-102', 'A-103', 'A-104', 'A-105')
            matches, issues = check_source_pages(str(root), pages(*names))
            self.assertEqual([name for name, ref in matches], [names[0]])
            self.assertEqual([name for name, reason in issues], list(names[1:]))
            self.assertIn('ambiguous', issues[0][1])
            self.assertIn('password protected', issues[2][1])
            self.assertIn('no PDF stem', issues[3][1])

    def test_labels_select_the_right_source_page_and_files_require_one_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, sources = fixture(root)
            (sources / 'A-101.pdf').unlink()
            make_set(sources / 'A-101.pdf')
            matches, issues = check_source_pages(str(root), pages('A-101'))
            self.assertFalse(matches)
            self.assertIn('expected one PDF per sheet', issues[0][1])
            matches, issues = check_source_pages(str(root), pages('A-101', 'A-102'), labels=True)
            self.assertFalse(issues)
            self.assertEqual([reference[1] for name, reference in matches], [1, 0])

    def test_custom_unicode_source_folder_is_independent_of_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Prøyecto 測試'
            manifest, original_output, sources = fixture(root)
            output = Path(tmp) / 'Deliveries' / 'final.pdf'
            output.parent.mkdir()
            before = (sources / 'A-101.pdf').read_bytes()
            manifest.write_text('SourceRootJSON: ' + json.dumps(str(root), ensure_ascii=True) + '\n'
                                + manifest.read_text(encoding='utf-8').replace(str(original_output), str(output)),
                                encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                b.build(str(manifest))
            self.assertFalse(manifest.exists())
            self.assertFalse(original_output.exists())
            self.assertEqual((sources / 'A-101.pdf').read_bytes(), before)
            with b.fitz.open(output) as doc:
                self.assertEqual(len(doc), 1)
                self.assertIn('SEARCHABLE SOURCE SHEET', doc[0].get_text())
                self.assertIn('500 LN FT', doc[0].get_text())

    def test_missing_source_preserves_existing_output_and_reports_all_sheets(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, output, sources = fixture(Path(tmp), missing=True)
            (sources / 'A-101.pdf').write_bytes(b'broken')
            output.write_bytes(b'previous export must survive')
            with self.assertRaisesRegex(ValueError, 'Source PDF check failed'):
                b.build(str(manifest))
            self.assertEqual(output.read_bytes(), b'previous export must survive')
            report = Path(str(output) + '.report.txt').read_text()
            self.assertIn('A-101:', report)
            self.assertIn('MISSING:', report)

    def test_source_becoming_unavailable_during_import_still_stops_entire_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, output, _ = fixture(Path(tmp))
            output.write_bytes(b'previous export')
            with patch.object(b, 'append_source_pdf', side_effect=OSError('Source disconnected')):
                with self.assertRaisesRegex(ValueError, 'Source disconnected'):
                    b.build(str(manifest))
            self.assertEqual(output.read_bytes(), b'previous export')

    def test_tiff_alignment_is_checked_before_any_pdf_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, output, sources = fixture(Path(tmp))
            (sources / 'A-101.pdf').unlink()
            make_source(sources / 'A-101.pdf', rotation=90)
            with patch.object(b, 'append_source_pdf') as append:
                with self.assertRaisesRegex(ValueError, 'aspect/orientation'):
                    b.build(str(manifest))
            append.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
