# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pdf_takeoff_builder as b


def make_source(path, rotation=0, crop=False, pages=1):
    with b.fitz.open() as doc:
        page = doc.new_page(width=800, height=600)
        page.insert_text((100, 100), 'SEARCHABLE SOURCE SHEET', fontsize=16, color=(0, 0, 1))
        page.draw_rect(b.fitz.Rect(180, 180, 300, 280), color=(0, 0, 1), width=2)
        if crop:
            page.set_cropbox(b.fitz.Rect(50, 50, 750, 550))
        page.set_rotation(rotation)
        for _ in range(pages - 1):
            doc.new_page()
        doc.save(path)


def fixture(folder, mode='PDF', greyscale=False, missing=False):
    folder.mkdir(parents=True, exist_ok=True)
    sources = folder / 'Plans & Docs' / '2026-01-01' / 'Permit set'
    sources.mkdir(parents=True, exist_ok=True)
    make_source(sources / 'A-101.pdf')
    Image.new('RGB', (1600, 1200), 'white').save(folder / '{PAGE}.tiff', dpi=(144, 144))
    (folder / 'Data.xml').write_text(
        '<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{PAGE}"/></Item>')
    output = folder / 'export.pdf'
    lines = ['OutputPDF: ' + str(output),
             'IncludeLegend: True', 'Greyscale: ' + str(greyscale)]
    if mode:
        lines.append('BackgroundMode: ' + mode)
    for name in (['A-101', 'MISSING'] if missing else ['A-101']):
        lines += ['--- PAGE_START ---', 'PageName: ' + name,
                  r'TakeoffPath: ..\Pages\Plans & Docs\2026-01-01\Permit set',
                  'PageFolder: ' + str(folder), 'PageMultiplier: 2',
                  '--- ITEM_START ---', 'ItemGUID: TRIM', 'ItemName: Trim measurement',
                  'ItemType: Linear', 'Color: 255', 'LineWidth: 2',
                  'Units: LN FT', 'PageTotal: 500', 'PageTotalBasis: Qty',
                  '--- SECTION_START ---', 'Closed: False', 'DigitizerData:',
                  '<Points><Point X="360" Y="560"/><Point X="600" Y="560"/></Points>',
                  '--- SECTION_END ---', '--- ITEM_END ---', '--- PAGE_END ---']
    manifest = folder / 'export.manifest.txt'
    manifest.write_text('\n'.join(lines), encoding='utf-8')
    return manifest, output, sources


class SourcePDFTests(unittest.TestCase):
    def test_filename_matches_are_unambiguous_and_sheet_delimited(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in ('A-10.pdf', 'A-1 - plan.pdf'):
                (folder / name).touch()
            self.assertEqual(Path(b.resolve_source_pdf(temp, 'A-1')[0]).name, 'A-1 - plan.pdf')
            (folder / 'A-1 - revised.pdf').touch()
            self.assertIn('ambiguous', b.resolve_source_pdf(temp, 'A-1')[1])
            (folder / 'A-1.pdf').touch()
            self.assertEqual(Path(b.resolve_source_pdf(temp, 'a-1')[0]).name, 'A-1.pdf')
            self.assertIsNone(b.resolve_source_pdf(temp, 'A-')[0])
            self.assertIsNone(b.resolve_source_pdf(temp, '')[0])

    def test_rotations_and_crop_preserve_appearance_and_text(self):
        with tempfile.TemporaryDirectory() as temp:
            for rotation in (0, 90, 180, 270):
                path = Path(temp) / f'rotated-{rotation}.pdf'
                make_source(path, rotation=rotation, crop=True)
                with b.fitz.open(path) as original, b.fitz.open() as out:
                    rect = original[0].rect
                    b.append_source_pdf(out, str(path),
                                        {'PageWidthPx': rect.width * 2,
                                         'PageHeightPx': rect.height * 2}, False)
                    self.assertEqual(out[0].rect, rect)
                    self.assertEqual(out[0].rotation, 0)
                    self.assertIn('SEARCHABLE SOURCE SHEET', out[0].get_text())
                    # Compare pixels as well as text: wrong rotation/crop can
                    # leave searchable text while drawing it in the wrong place.
                    original_pixels = original[0].get_pixmap().samples
                    exported_pixels = out[0].get_pixmap().samples
                    error = sum(abs(a-z) for a, z in zip(original_pixels, exported_pixels))
                    self.assertLess(error / len(original_pixels), 0.1)

    def test_rejects_multisheet_and_incompatible_aspect(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'source.pdf'
            make_source(path, pages=2)
            with b.fitz.open() as out:
                with self.assertRaisesRegex(ValueError, 'expected one PDF'):
                    b.append_source_pdf(out, str(path), {}, False)
                self.assertEqual(len(out), 0)
            path.unlink()
            make_source(path)
            with b.fitz.open() as out:
                with self.assertRaisesRegex(ValueError, 'aspect/orientation'):
                    b.append_source_pdf(out, str(path), {'PageWidthPx': 600, 'PageHeightPx': 800}, False)
                self.assertEqual(len(out), 0)

    def test_pdf_export_alignment_searchability_colors_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            for grey in (False, True):
                folder = Path(temp) / str(grey)
                manifest, output, sources = fixture(folder, greyscale=grey)
                before = (sources / 'A-101.pdf').read_bytes()
                with contextlib.redirect_stdout(io.StringIO()):
                    b.build(str(manifest))
                self.assertEqual((sources / 'A-101.pdf').read_bytes(), before)
                self.assertFalse(manifest.exists())
                with b.fitz.open(output) as doc:
                    page = doc[0]
                    self.assertEqual(page.rect, b.fitz.Rect(0, 0, 800, 600))
                    self.assertEqual(page.get_images(), [])  # no rasterized drawing
                    self.assertIn('SEARCHABLE SOURCE SHEET', page.get_text())
                    self.assertIn('500 LN FT', page.get_text())
                    self.assertIn('Trim measurement', page.get_text())
                    page_fonts = {f[4] for f in page.get_fonts(full=True) if f[6] == 0}
                    self.assertTrue({'helv', 'hebo'} <= page_fonts)
                    self.assertIn('PAGE MULTIPLIER: 2', page.get_text())
                    paths = page.get_drawings()
                    source = next(d for d in paths if d['rect'] == b.fitz.Rect(180, 180, 300, 280))
                    if grey:
                        # MuPDF's gray-to-RGB reporting has tiny channel differences.
                        self.assertAlmostEqual(source['color'][0], source['color'][1], places=3)
                        self.assertAlmostEqual(source['color'][1], source['color'][2], places=3)
                    else:
                        self.assertEqual(source['color'], (0, 0, 1))
                    overlay = next(d for d in paths if d['rect'] == b.fitz.Rect(180, 280, 300, 280))
                    self.assertEqual(overlay['color'], (1, 0, 0))

    def test_missing_pdf_keeps_diagnostics_and_never_falls_back_to_tiff(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, output, _ = fixture(Path(temp), missing=True)
            with self.assertRaisesRegex(ValueError, 'Source PDF check failed'):
                b.build(str(manifest))
            self.assertTrue(manifest.exists())
            self.assertIn('MISSING', Path(str(output) + '.report.txt').read_text())
            self.assertFalse(output.exists())

    def test_raster_default_and_explicit_mode_remain_raster(self):
        with tempfile.TemporaryDirectory() as temp:
            for mode in ('', 'Raster'):
                manifest, output, _ = fixture(Path(temp) / (mode or 'default'), mode=mode)
                with contextlib.redirect_stdout(io.StringIO()):
                    b.build(str(manifest))
                with b.fitz.open(output) as doc:
                    self.assertTrue(doc[0].get_images())
                    self.assertNotIn('SEARCHABLE SOURCE SHEET', doc[0].get_text())
                    self.assertIn('500 LN FT', doc[0].get_text())

    def test_print_only_source_stays_color_when_greyscale_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.pdf'
            make_source(source)
            with b.fitz.open() as out:
                b.append_source_pdf(out, str(source), {'PageWidthPx': 800, 'PageHeightPx': 600}, True)
                self.assertEqual(out[0].get_drawings()[0]['color'], (0, 0, 1))

    def test_export_cannot_overwrite_a_source_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, output, sources = fixture(Path(temp))
            source = sources / 'A-101.pdf'
            before = source.read_bytes()
            manifest.write_text(manifest.read_text().replace(str(output), str(source))
                                + '\n')
            # Keep the selected project root when deliberately choosing a
            # conflicting output inside the nested source folder.
            manifest.write_text('ProjectRoot: ' + str(Path(temp)) + '\n' + manifest.read_text())
            with self.assertRaisesRegex(ValueError, 'overwrite a source PDF'):
                b.build(str(manifest))
            self.assertEqual(before, source.read_bytes())

    def test_project_tree_selects_the_correct_document_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for doc_name in ('Permit set', 'Architectural set'):
                folder = root / 'Plans & Docs' / '2026-01-01' / doc_name
                folder.mkdir(parents=True)
                (folder / 'IDC.1.pdf').touch()
            folder, err = b.resolve_page_folder(
                str(root), r'..\Pages\Plans & Docs\2026-01-01\Permit set')
            self.assertIsNone(err)
            path, err = b.resolve_source_pdf(folder, 'IDC.1')
            self.assertIsNone(err)
            self.assertEqual(Path(path), root / 'Plans & Docs' / '2026-01-01' / 'Permit set' / 'IDC.1.pdf')
            self.assertIsNone(b.resolve_page_folder(str(root), r'..\Pages\..\wrong')[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
