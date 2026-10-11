# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image
import pdf_takeoff_builder as b


class MultiplierTests(unittest.TestCase):
    display = b.fitz.Rect(0, 0, 1000, 700)

    def place(self, obstacles):
        return b.multiplier_badge_position(self.display, 200, 40, obstacles)

    def test_default_and_alternative_corners(self):
        corners = [b.fitz.Rect(40, 40, 240, 80),
                   b.fitz.Rect(760, 40, 960, 80),
                   b.fitz.Rect(760, 620, 960, 660),
                   b.fitz.Rect(40, 620, 240, 660)]
        for i, expected in enumerate(corners):
            self.assertEqual(self.place(corners[:i]), expected)

    def test_clearance_and_individual_sections(self):
        # A nearby stroke just outside the badge still needs clearance.
        blocker = b.fitz.Rect(243, 45, 248, 65)
        self.assertEqual(self.place([blocker]).x0, 760)
        # Independent sections at opposite edges don't block the whole page.
        parts = [b.fitz.Rect(0, 300, 20, 350), b.fitz.Rect(980, 300, 1000, 350)]
        self.assertEqual(self.place(parts).tl, b.fitz.Point(40, 40))

    def test_occupied_corners_find_clear_edge_space(self):
        obstacles = [b.fitz.Rect(0, 0, 250, 100), b.fitz.Rect(750, 0, 1000, 100),
                     b.fitz.Rect(0, 600, 250, 700), b.fitz.Rect(750, 600, 1000, 700)]
        box = self.place(obstacles)
        self.assertIsNotNone(box)
        self.assertTrue(self.display.contains(box))
        self.assertTrue(all(not box.intersects(r + b.fitz.Rect(-6, -6, 6, 6))
                            for r in obstacles))

    def test_no_clear_space_does_not_draw_over_content(self):
        self.assertIsNone(self.place([self.display]))

    def test_rotated_page_uses_display_coordinates(self):
        with b.fitz.open() as doc:
            page = doc.new_page(width=700, height=1000)
            page.set_rotation(90)
            obstacle = b.fitz.Rect(0, 0, 500, 200)
            box = b.draw_multiplier_badge(page, page.rect, page.derotation_matrix,
                                          '7', [obstacle])
            self.assertGreater(box.x0, 500)
            self.assertIn('PAGE MULTIPLIER: 7', page.get_text())
            rendered = page.get_drawings()[0]['rect'] * page.rotation_matrix
            for a, z in zip(rendered, box):
                self.assertAlmostEqual(a, z, places=3)

    def test_full_export_avoids_manual_legend_and_count_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            verify_export(Path(temp))


def verify_export(folder):
    folder.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (1100, 700), 'white').save(folder / '{BADGE}.tiff', dpi=(72, 72))
    (folder / 'Data.xml').write_text(
        '<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{BADGE}"/></Item>')
    output = folder / 'multiplier-relocation.pdf'
    lines = ['OutputPDF: ' + str(output), 'IncludeLegend: True', 'Greyscale: False']
    cases = [[(1030, 50)], [(1030, 50), (1030, 650)], [(550, 350)]]
    for i, points in enumerate(cases):
        legend = ((20, 20, 500, 220) if i < 2 else (580, 400, 1050, 650))
        lines += ['--- PAGE_START ---', f'PageName: BADGE-{i+1}',
                  'PageFolder: ' + str(folder), 'ScaleX: 1', 'ScaleY: 1',
                  'PageMultiplier: 7', 'LegendData:',
                  f'<Points><Point X="{legend[0]}" Y="{legend[1]}"/>'
                  f'<Point X="{legend[2]}" Y="{legend[3]}"/></Points>', 'LegendDataEnd',
                  '--- ITEM_START ---', 'ItemGUID: COUNT',
                  'ItemName: Count takeoff section used to check multiplier placement',
                  'ItemType: Count', 'Color: 255', 'Units: EA', f'PageTotal: {len(points)}',
                  '--- SECTION_START ---', 'Closed: False', 'DigitizerData:',
                  '<Points>' + ''.join(f'<Point X="{x}" Y="{y}"/>' for x,y in points) + '</Points>',
                  '--- SECTION_END ---', '--- ITEM_END ---', '--- PAGE_END ---']
    manifest = folder / 'sample.manifest.txt'
    manifest.write_text('\n'.join(lines), encoding='utf-8')
    with contextlib.redirect_stdout(io.StringIO()):
        b.build(str(manifest))
    with b.fitz.open(output) as doc:
        assert len(doc) == 3
        for i, page in enumerate(doc):
            drawings = page.get_drawings()
            badge = next(d['rect'] for d in drawings if d['fill'] is not None
                         and all(abs(a-z)<1e-5 for a,z in zip(d['fill'], b.MULT_BG_COLOR)))
            legend = next(d['rect'] for d in drawings if d['fill'] == (1,1,1))
            assert not badge.intersects(legend + b.fitz.Rect(-6,-6,6,6))
            for x,y in cases[i]:
                assert not badge.intersects(b.fitz.Rect(x-12.5,y-12.5,x+12.5,y+12.5))
            assert 'PAGE MULTIPLIER: 7' in page.get_text()
            if i == 0: assert badge.x0 > 550 and badge.y0 > 350
            elif i == 1: assert badge.x0 == 40 and badge.y0 > 350
            else: assert badge.tl == b.fitz.Point(40,40)
        doc[1].get_pixmap().save(folder / 'multiplier-relocation.png')


if __name__ == '__main__':
    unittest.main(verbosity=2)
