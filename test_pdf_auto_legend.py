# SPDX-License-Identifier: AGPL-3.0-only
import unittest
import re
import tempfile
from unittest.mock import patch
from pathlib import Path
import pdf_takeoff_builder as b


class AutomaticLegendTests(unittest.TestCase):
    def layout(self, bounds, width=lambda fs: 20 * fs + 16,
               height=lambda fs: 10 * fs + 24, display=None):
        return b.automatic_legend_layout(
            display or b.fitz.Rect(0, 0, 1000, 700), bounds, width, height)

    def test_space_above_tracings_is_never_used(self):
        bounds = b.fitz.Rect(100, 260, 960, 650)
        self.assertIsNone(self.layout(bounds))
        self.assertIsNone(self.layout(b.fitz.Rect(100, 600, 960, 650)))

    def test_uses_space_to_right(self):
        bounds = b.fitz.Rect(100, 25, 500, 650)
        fs, box = self.layout(bounds)
        self.assertGreater(fs, 6)
        self.assertGreaterEqual(box.x0, bounds.x1 + b.AUTO_LEGEND_CLEARANCE)
        self.assertEqual(box.x1, 980)

    def test_crowded_page_returns_explicit_failure(self):
        self.assertIsNone(self.layout(b.fitz.Rect(10, 10, 990, 690)))

    def adaptive(self, bounds, rows=4, width=lambda fs: 30 * fs + 20,
                 height=lambda fs: 8 * fs + 24):
        return b.adaptive_legend_layout(b.fitz.Rect(0, 0, 1000, 700),
                                        bounds, rows, width, height)

    def test_lower_half_widens_at_twelve(self):
        bounds = b.fitz.Rect(100, 400, 920, 650)
        (fs, box), rule = self.adaptive(bounds)
        self.assertEqual(fs, 12)
        self.assertEqual(box.y0, 20)
        self.assertEqual(box.x1, 980)
        self.assertLess(box.x0, bounds.x1)
        self.assertFalse(box.intersects(bounds))
        self.assertIn('wider top-right', rule)

    def test_upper_half_also_uses_top_right(self):
        (fs, box), rule = self.adaptive(b.fitz.Rect(100, 200, 920, 300))
        self.assertEqual(fs, 12)
        self.assertEqual(box.x1, 980)
        self.assertEqual(box.y0, 20)
        self.assertIn('wider top-right', rule)

    def test_upper_half_collision_keeps_right_strip(self):
        (fs, box), rule = self.adaptive(b.fitz.Rect(100, 100, 920, 300))
        self.assertLess(fs, 12)
        self.assertGreaterEqual(box.x0, 932)
        self.assertEqual(box.y0, 20)

    def test_mixed_half_or_six_rows_keep_right_strip(self):
        for bounds, rows in [(b.fitz.Rect(100, 200, 920, 600), 4),
                             (b.fitz.Rect(100, 400, 920, 650), 6)]:
            (fs, box), rule = self.adaptive(bounds, rows)
            self.assertLess(fs, 12)
            self.assertGreaterEqual(box.x0, 932)
            self.assertEqual(rule, 'right-strip')

    def test_corner_too_wide_or_overlapping_uses_standard(self):
        bounds = b.fitz.Rect(100, 400, 920, 650)
        for width, height in [(lambda fs: 100 * fs, lambda fs: 10 * fs),
                              (lambda fs: 30 * fs + 20, lambda fs: 40 * fs)]:
            (fs, box), rule = self.adaptive(bounds, width=width, height=height)
            self.assertLess(fs, 12)
            self.assertGreaterEqual(box.x0, 932)
            self.assertIn('did not fit', rule)

    def test_lower_half_preserves_existing_large_legend(self):
        (fs, box), rule = self.adaptive(b.fitz.Rect(100, 400, 200, 600))
        self.assertEqual(fs, 24)
        self.assertEqual(rule, 'right-strip')

    def test_narrow_strip_shrinks_below_six_points(self):
        bounds = b.fitz.Rect(100, 50, 920, 650)
        fs, box = self.layout(bounds)
        self.assertGreater(fs, 0)
        self.assertLess(fs, 6)
        self.assertAlmostEqual(fs, 1.6, places=5)
        self.assertGreaterEqual(box.x0, bounds.x1 + b.AUTO_LEGEND_CLEARANCE)
        self.assertEqual(box.x1, 980)

    def test_many_rows_can_shrink_below_six(self):
        fs, box = self.layout(None, height=lambda fs: 200 * fs + 24)
        self.assertGreater(fs, 0)
        self.assertLess(fs, 6)
        self.assertLessEqual(box.y1, 690)

    def test_long_names_and_many_rows_reduce_font(self):
        normal = self.layout(None)[0]
        self.assertLess(self.layout(None, width=lambda fs: 100 * fs)[0], normal)
        self.assertLess(self.layout(None, height=lambda fs: 80 * fs)[0], normal)

    def test_empty_and_rotated_display(self):
        for display in (b.fitz.Rect(0, 0, 1000, 700),
                        b.fitz.Rect(0, 0, 700, 1000)):
            fs, box = self.layout(None, display=display)
            self.assertEqual(fs, b.AUTO_LEGEND_MAX_FONT)
            self.assertTrue(display.contains(box))

    def test_complete_export_preserves_explicit_legend_and_enlarges_fallback(self):
        from sample_fixture import render_sample
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch('os.remove'):
                render_sample(folder)  # Also checks existing explicit legend colors.
            manifest = folder / 'sample_manifest.txt'
            text = re.sub(r'LegendData:\n.*?LegendDataEnd\n', '',
                          manifest.read_text(), flags=re.S)
            manifest.write_text(text)
            b.build(str(manifest))
            with b.fitz.open(folder / 'export_color_test.pdf') as doc:
                for page in doc:
                    title = next(span for block in page.get_text('dict')['blocks']
                                 if 'lines' in block for line in block['lines']
                                 for span in line['spans']
                                 if span['text'].startswith('TAKEOFF SUMMARY'))
                    self.assertGreater(title['size'], 6)
                    boxes = [d['rect'] for d in page.get_drawings()
                             if d['fill'] == (1, 1, 1)]
                    legend = next(r for r in boxes if r.contains(b.fitz.Rect(title['bbox'])))
                    self.assertGreaterEqual(legend.x0, 480 + 1 + b.AUTO_LEGEND_CLEARANCE)
                    self.assertTrue(page.rect.contains(legend))

            # Move the rightmost tracing near the edge, with ample space above:
            # the legend must go to a separate page, never into that top space.
            text = text.replace('X="480"', 'X="1070"')
            manifest.write_text(text)
            b.build(str(manifest))
            with b.fitz.open(folder / 'export_color_test.pdf') as doc:
                self.assertEqual(len(doc), 4)
                for index in (0, 2):
                    self.assertNotIn('TAKEOFF SUMMARY', doc[index].get_text())
                    self.assertIn('PAGE MULTIPLIER', doc[index].get_text())
                    self.assertIn('TAKEOFF SUMMARY', doc[index + 1].get_text())
                    self.assertIn('Original item', doc[index + 1].get_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
