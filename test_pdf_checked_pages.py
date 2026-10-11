# SPDX-License-Identifier: AGPL-3.0-only
"""Export contracts used by the checkbox page picker.

The PlanSwift dialog still needs a host-side test. These tests exercise the
real builder with empty checked pages and background-only page manifests.
"""
import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest

import pdf_takeoff_builder as b
from test_pdf_source_background import fixture


def without_items(text):
    return re.sub(r'--- ITEM_START ---.*?--- ITEM_END ---', '', text, flags=re.S)


class CheckedPageExportTests(unittest.TestCase):
    def test_drawing_only_has_background_without_legend_badge_or_layers(self):
        for mode in ('PDF', 'Raster'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                manifest, output, sources = fixture(Path(temp), mode=mode)
                manifest.write_text(without_items(manifest.read_text()).replace(
                    'IncludeLegend: True', 'IncludeLegend: False'))
                with contextlib.redirect_stdout(io.StringIO()):
                    b.build(str(manifest))
                with b.fitz.open(output) as doc:
                    self.assertEqual(len(doc), 1)
                    self.assertEqual(doc.get_ocgs(), {})
                    self.assertEqual(doc[0].annot_xrefs(), [])
                    self.assertNotIn('TAKEOFF SUMMARY', doc[0].get_text())
                    self.assertNotIn('PAGE MULTIPLIER', doc[0].get_text())
                    if mode == 'PDF':
                        with b.fitz.open(sources / 'A-101.pdf') as source:
                            self.assertEqual(doc[0].get_text(), source[0].get_text())
                            self.assertEqual(doc[0].get_pixmap().samples,
                                             source[0].get_pixmap().samples)
                    else:
                        self.assertTrue(doc[0].get_images())
                        self.assertEqual(set(doc[0].get_pixmap().samples), {255})
                self.assertFalse(manifest.exists())
                self.assertFalse(Path(str(output) + '.report.txt').exists())

    def test_checked_empty_page_is_kept_in_order_even_with_legend_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, output, _ = fixture(Path(temp), mode='PDF')
            header, page = manifest.read_text().split('--- PAGE_START ---', 1)
            # Repeated sheet names are deliberate: checked pages must not be
            # collapsed by caption or dropped because one has no takeoff.
            manifest.write_text(header + '--- PAGE_START ---' + without_items(page)
                                + '\n--- PAGE_START ---' + page)
            with contextlib.redirect_stdout(io.StringIO()):
                b.build(str(manifest))
            with b.fitz.open(output) as doc:
                self.assertEqual(len(doc), 2)
                self.assertIn('SEARCHABLE SOURCE SHEET', doc[0].get_text())
                self.assertNotIn('Trim measurement', doc[0].get_text())
                self.assertIn('Trim measurement', doc[1].get_text())
                self.assertIn('500 LN FT', doc[1].get_text())


if __name__ == '__main__':
    unittest.main()
