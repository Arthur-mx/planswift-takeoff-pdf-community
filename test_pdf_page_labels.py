# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pdf_takeoff_builder as b
from pdf_source_labels import read_page_labels, source_label_index, match_source_label
from test_pdf_source_background import fixture


def make_set(path, labels=('A-102', 'A-101'), rotation=0, crop=False):
    with b.fitz.open() as doc:
        for label in labels:
            page = doc.new_page(width=800, height=600)
            page.insert_text((100, 100), 'SOURCE ' + label, fontsize=24)
            page.draw_rect(b.fitz.Rect(180, 180, 300, 280), color=(0, 0, 1), width=2)
            if crop:
                page.set_cropbox(b.fitz.Rect(50, 50, 750, 550))
            page.set_rotation(rotation)
        doc.set_page_labels([{'startpage': i, 'prefix': label, 'style': ''}
                             for i, label in enumerate(labels)])
        doc.save(path)


class PageLabelTests(unittest.TestCase):
    def test_exact_unique_labels_match_both_supported_folder_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'Permit set'
            make_set(root / 'Permit set.PDF')
            index, error = source_label_index(folder)
            self.assertIsNone(error)
            ref, error = match_source_label(index, ' a-101 ')
            self.assertEqual(ref[1:], (1, 'A-101'))
            self.assertIsNone(match_source_label(index, 'A-10')[0])
            folder.mkdir()
            make_set(folder / 'Second set.pdf', labels=('IDC.1',))
            index, error = source_label_index(folder)
            self.assertIsNone(error)
            self.assertEqual(match_source_label(index, 'IDC.1')[0][1], 0)

    def test_duplicate_labels_in_one_or_multiple_sets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_set(root / 'one.pdf', labels=('A-101', 'a-101'))
            index, error = source_label_index(root)
            self.assertIsNone(error)
            self.assertIn('duplicate', match_source_label(index, 'A-101')[1])
            make_set(root / 'two.pdf', labels=('A-102',))
            make_set(root / 'three.pdf', labels=('A-102',))
            index, error = source_label_index(root)
            self.assertIn('duplicate', match_source_label(index, 'A-102')[1])

    def test_unlabeled_pages_and_unreadable_candidates_never_use_page_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with b.fitz.open() as doc:
                doc.new_page()
                doc.save(root / 'A-101.pdf')
            with self.assertRaisesRegex(ValueError, 'no page labels'):
                read_page_labels(root / 'A-101.pdf')
            index, error = source_label_index(root)
            self.assertEqual(index, {})
            self.assertIn('no page labels', error)
            (root / 'A-101.pdf').unlink()
            make_set(root / 'blank-label.pdf', labels=('A-101', ''))
            with self.assertRaisesRegex(ValueError, 'Missing page labels'):
                read_page_labels(root / 'blank-label.pdf')
            (root / 'blank-label.pdf').unlink()
            (root / 'broken.pdf').write_bytes(b'broken')
            make_set(root / 'good.pdf')
            index, error = source_label_index(root)
            self.assertEqual(index, {})
            self.assertIn('broken.pdf', error)

    def test_labels_do_not_search_other_revision_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'old').mkdir()
            (root / 'new').mkdir()
            make_set(root / 'old' / 'set.pdf', labels=('A-101',))
            make_set(root / 'new' / 'set.pdf', labels=('A-102',))
            index, error = source_label_index(root / 'new')
            self.assertIsNone(error)
            self.assertIn('no page label', match_source_label(index, 'A-101')[1])

    def test_nonfirst_page_rotation_crop_and_text_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rotation in (0, 90, 180, 270):
                path = Path(tmp) / f'{rotation}.pdf'
                make_set(path, rotation=rotation, crop=True)
                with b.fitz.open(path) as original, b.fitz.open() as out:
                    rect = original[1].rect
                    b.append_source_pdf(out, str(path), {'PageWidthPx': rect.width * 2,
                                        'PageHeightPx': rect.height * 2}, False, 1, 'A-101')
                    self.assertIn('SOURCE A-101', out[0].get_text())
                    self.assertNotIn('SOURCE A-102', out[0].get_text())
                    self.assertEqual(out[0].rect, rect)
                    first = original[1].get_pixmap().samples
                    second = out[0].get_pixmap().samples
                    self.assertEqual(len(first), len(second))
                    self.assertLess(sum(abs(a - z) for a, z in zip(first, second)) / len(first), 0.1)
                    self.assertEqual(out[0].get_images(), [])

    def test_export_uses_label_not_source_order_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, output, sources = fixture(Path(tmp))
            (sources / 'A-101.pdf').unlink()
            source = sources / 'Drawing set.pdf'
            make_set(source)
            before = source.read_bytes()
            header, section = manifest.read_text().split('--- PAGE_START ---', 1)
            manifest.write_text('SourcePDFMode: Labels\n' + header + '--- PAGE_START ---' + section
                                + '\n--- PAGE_START ---' + section.replace('PageName: A-101', 'PageName: A-102'))
            with contextlib.redirect_stdout(io.StringIO()):
                b.build(str(manifest))
            self.assertFalse(manifest.exists())
            self.assertEqual(before, source.read_bytes())
            with b.fitz.open(output) as doc:
                self.assertEqual(len(doc), 2)
                self.assertIn('SOURCE A-101', doc[0].get_text())
                self.assertIn('SOURCE A-102', doc[1].get_text())
                self.assertIn('500 LN FT', doc[0].get_text())
                self.assertTrue(doc.get_ocgs())

    def test_missing_label_reports_sheet_without_raster_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, output, sources = fixture(Path(tmp), missing=True)
            (sources / 'A-101.pdf').unlink()
            make_set(sources / 'Drawing set.pdf')
            manifest.write_text('SourcePDFMode: Labels\n' + manifest.read_text())
            with self.assertRaisesRegex(ValueError, 'Source PDF check failed'):
                b.build(str(manifest))
            self.assertTrue(manifest.exists())
            self.assertIn('no page label matches', Path(str(output) + '.report.txt').read_text())
            self.assertFalse(output.exists())

    def test_output_cannot_overwrite_labeled_source_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, output, sources = fixture(Path(tmp))
            (sources / 'A-101.pdf').unlink()
            source = sources / 'Drawing set.pdf'
            make_set(source)
            before = source.read_bytes()
            manifest.write_text('SourcePDFMode: Labels\nProjectRoot: ' + tmp + '\n'
                                + manifest.read_text().replace(str(output), str(source)))
            with self.assertRaisesRegex(ValueError, 'overwrite a source PDF'):
                b.build(str(manifest))
            self.assertEqual(before, source.read_bytes())


if __name__ == '__main__':
    unittest.main()
