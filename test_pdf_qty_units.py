# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image
import pdf_takeoff_builder as b


class QuantityLegendTests(unittest.TestCase):
    def test_calculated_quantities_and_units_survive_export(self):
        # Simulate the new manifest: 50 count points, but allocated Qty = 500
        # LN FT. Also cover counts yielding area, ordinary EA, and a linear
        # formula yielding area (which must not be compared to drawn length).
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            Image.new('RGB', (1100, 700), 'white').save(
                folder / '{QTY}.tiff', dpi=(72, 72))
            (folder / 'Data.xml').write_text(
                '<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{QTY}"/></Item>')
            output = folder / 'qty.pdf'
            lines = ['OutputPDF: ' + str(output), 'IncludeLegend: True']
            cases = [('Trim pieces', 'Count', '500', 'LN FT'),
                     ('Count area', 'Count', '500', 'SQ FT'),
                     ('Individual pieces', 'Count', '50', 'EA'),
                     ('Calculated wall', 'Linear', '750', 'SQ FT'),
                     ('Unknown units', 'Count', '25', '')]
            for page in range(2):
                lines += ['--- PAGE_START ---', 'PageName: QTY-' + str(page + 1),
                          'PageFolder: ' + str(folder), 'ScaleX: 1', 'ScaleY: 1']
                for i, (name, kind, total, units) in enumerate(cases):
                    points = ''.join(f'<Point X="{80 + j * 7}" Y="{150 + i * 75}"/>'
                                     for j in range(50))
                    lines += ['--- ITEM_START ---', 'ItemGUID: ' + str(i),
                              'ItemName: ' + name, 'ItemType: ' + kind, 'Color: 255',
                              'PageTotal: ' + total, 'PageTotalBasis: Qty',
                              'Units: ' + units, '--- SECTION_START ---',
                              'Closed: False', 'DigitizerData:',
                              '<Points>' + points + '</Points>',
                              '--- SECTION_END ---', '--- ITEM_END ---']
                lines += ['--- PAGE_END ---']
            manifest = folder / 'manifest.txt'
            manifest.write_text('\n'.join(lines), encoding='utf-8')
            log = io.StringIO()
            report_path = Path(str(output) + '.report.txt')
            report_path.write_text('Old report to be removed', encoding='utf-8')
            with contextlib.redirect_stdout(log):
                b.build(str(manifest))
            with b.fitz.open(output) as doc:
                self.assertEqual(len(doc), 2)
                for page in doc:
                    text = page.get_text()
                    for expected in ('500 LN FT', '500 SQ FT', '50 EA', '750 SQ FT'):
                        self.assertIn(expected, text)
                    self.assertNotIn('25 EA', text)
                    self.assertNotIn('50 LN FT', text)
            self.assertNotIn('delta', log.getvalue())
            self.assertIn('Qty inputUnits missing for Unknown units', log.getvalue())
            self.assertFalse(manifest.exists())
            self.assertFalse(report_path.exists())
            self.assertIn('mode=automatic', log.getvalue())
            self.assertIn('final PDF page=2', log.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
