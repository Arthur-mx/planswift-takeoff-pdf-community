# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest

import pdf_takeoff_builder as b
from test_pdf_source_background import fixture


def extra_item(guid, name, kind, points):
    return '\n'.join(['--- ITEM_START ---', 'ItemGUID: ' + guid, 'ItemName: ' + name,
                      'ItemType: ' + kind, 'Color: 65280', 'PageTotal: 10',
                      'Units: EA' if kind == 'Count' else 'Units: SQ FT', 'PageTotalBasis: Qty',
                      '--- SECTION_START ---', 'Closed: ' + str(kind == 'Area'), 'DigitizerData:',
                      '<Points>' + ''.join(f'<Point X="{x}" Y="{y}"/>' for x, y in points) + '</Points>',
                      '--- SECTION_END ---', '--- ITEM_END ---', ''])


def selection_fixture(folder, mode='PDF'):
    manifest, output, _ = fixture(folder, mode=mode, greyscale=True)
    header, page = manifest.read_text().split('--- PAGE_START ---', 1)
    floor = extra_item('FLOOR', 'Floor finish', 'Area', [(100, 700), (300, 700), (300, 950), (100, 950)])
    count = extra_item('COUNT', 'Trim pieces', 'Count', [(500, 900), (570, 900)])
    late = extra_item('LATER', 'Later page only', 'Count', [(650, 900)])
    highlighter = '\n'.join(['--- HIGHLIGHTER_START ---', 'Color: 65535', 'HighlighterMode: Line',
                             'LineWidth: 10', 'DigitizerData:',
                             '<Points><Point X="100" Y="1000"/><Point X="300" Y="1000"/></Points>',
                             '--- HIGHLIGHTER_END ---', ''])
    page1 = page.replace('--- PAGE_END ---', floor + count + highlighter + '--- PAGE_END ---')
    page2 = page.replace('--- PAGE_END ---', late + '--- PAGE_END ---')
    manifest.write_text(header + '--- PAGE_START ---' + page1 + '\n--- PAGE_START ---' + page2)
    with contextlib.redirect_stdout(io.StringIO()):
        b.build(str(manifest))
    return output


def action(doc, pno, label):
    for xref, _, _ in doc[pno].annot_xrefs():
        if doc.xref_get_key(xref, 'Contents')[1] == label:
            return doc.xref_get_key(xref, 'A/State')[1]
    raise AssertionError('Missing legend action: ' + label)


def click(doc, pno, label):
    """Execute the saved native action's operations, then reopen for rendering."""
    state = {xref: info['on'] for xref, info in doc.get_ocgs().items()}
    operation = None
    for token in re.finditer(r'/(ON|OFF|Toggle)|(?:(\d+)\s+0\s+R)', action(doc, pno, label)):
        if token[1]:
            operation = token[1]
        else:
            xref = int(token[2])
            state[xref] = not state[xref] if operation == 'Toggle' else operation == 'ON'
    doc.set_layer(-1, on=[x for x in state if state[x]], off=[x for x in state if not state[x]])
    updated = b.fitz.open(stream=doc.tobytes(), filetype='pdf')
    doc.close()
    return updated


def pixels(doc, pno, rect=None):
    return doc[pno].get_pixmap(clip=rect).samples


class LegendSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = selection_fixture(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_selection_layers_start_hidden_unlisted_and_nonprinting(self):
        with b.fitz.open(self.path) as doc:
            groups = doc.get_ocgs()
            selections = {x for x, g in groups.items() if g['name'].startswith('Selection |')}
            self.assertEqual(len(selections), 5)
            self.assertTrue(all(not groups[x]['on'] for x in selections))
            panel = [row['text'] for row in doc.layer_ui_configs()]
            self.assertFalse(any(name.startswith('Selection |') for name in panel))
            self.assertIn('Highlighters', panel)
            for xref in selections:
                self.assertEqual(doc.xref_get_key(xref, 'Usage/Print/PrintState')[1], '/OFF')
                self.assertEqual(doc.xref_get_key(xref, 'Usage/Export/ExportState')[1], '/OFF')
            settings = doc.xref_get_key(doc.pdf_catalog(), 'OCProperties/D/AS')[1]
            self.assertIn('/Event/Print', re.sub(r'\s', '', settings))
            # All interactive rectangles fit and distinct buttons do not overlap.
            for page in doc:
                rects = []
                for xref, _, _ in page.annot_xrefs():
                    r = b.fitz.Rect([float(n) for n in doc.xref_get_key(xref, 'Rect')[1].strip('[]').split()])
                    r = r * page.transformation_matrix
                    self.assertTrue(page.rect.contains(r))
                    self.assertFalse(any(r.intersects(other) for other in rects))
                    rects.append(r)

    def test_selection_swaps_clears_and_is_local_to_each_page(self):
        doc = b.fitz.open(self.path)
        try:
            line = b.fitz.Rect(174, 274, 306, 286)
            area = b.fitz.Rect(44, 344, 156, 481)
            baseline = [pixels(doc, pno) for pno in (0, 1)]
            normal_line, normal_area = pixels(doc, 0, line), pixels(doc, 0, area)
            doc = click(doc, 0, 'Highlight on this page: Trim measurement')
            self.assertNotEqual(normal_line, pixels(doc, 0, line))
            self.assertEqual(baseline[1], pixels(doc, 1))
            doc = click(doc, 0, 'Highlight on this page: Floor finish')
            self.assertEqual(normal_line, pixels(doc, 0, line))
            self.assertNotEqual(normal_area, pixels(doc, 0, area))
            doc = click(doc, 1, 'Highlight on this page: Trim measurement')
            other_selected = pixels(doc, 1)
            doc = click(doc, 0, 'Clear highlight')
            self.assertEqual(baseline[0], pixels(doc, 0))
            self.assertEqual(other_selected, pixels(doc, 1))
            doc = click(doc, 1, 'Highlight on this page: Trim measurement')
            self.assertEqual(baseline[1], pixels(doc, 1))
        finally:
            doc.close()

    def test_global_buttons_include_later_items_and_exclude_highlighters(self):
        doc = b.fitz.open(self.path)
        try:
            baseline = [pixels(doc, pno) for pno in (0, 1)]
            doc = click(doc, 0, 'Highlight on this page: Floor finish')
            doc = click(doc, 1, 'Highlight on this page: Later page only')
            doc = click(doc, 0, 'Hide all')
            for _, group in doc.get_ocgs().items():
                self.assertEqual(group['on'], group['name'] == 'Highlighters')
            for page in doc:
                self.assertIn('Show all', page.get_text())
                self.assertIn('PAGE MULTIPLIER: 2', page.get_text())
                self.assertIn('Trim measurement', page.get_text())
            doc = click(doc, 0, 'Show all')
            for _, group in doc.get_ocgs().items():
                self.assertEqual(group['on'], not group['name'].startswith('Selection |'))
            self.assertEqual(baseline, [pixels(doc, pno) for pno in (0, 1)])
        finally:
            doc.close()

    def test_count_selection_and_eye_hide_show_do_not_leave_stale_highlights(self):
        doc = b.fitz.open(self.path)
        try:
            count_rect = b.fitz.Rect(238, 438, 297, 462)
            baseline = pixels(doc, 0, count_rect)
            doc = click(doc, 0, 'Highlight on this page: Trim pieces')
            self.assertNotEqual(baseline, pixels(doc, 0, count_rect))
            doc = click(doc, 0, 'Show/hide takeoff: Trim pieces')
            groups = doc.get_ocgs()
            self.assertFalse(any(g['on'] for g in groups.values() if g['name'].endswith('| Trim pieces')))
            doc = click(doc, 0, 'Show/hide takeoff: Trim pieces')
            self.assertEqual(baseline, pixels(doc, 0, count_rect))
            doc = click(doc, 0, 'Hide all')
            doc = click(doc, 0, 'Highlight on this page: Trim pieces')
            self.assertTrue(next(g['on'] for g in doc.get_ocgs().values() if g['name'] == 'Takeoff | Trim pieces'))
            self.assertNotEqual(baseline, pixels(doc, 0, count_rect))
        finally:
            doc.close()

    def test_raster_selection_uses_the_same_controls(self):
        output = selection_fixture(Path(self.temp.name) / 'raster', mode='Raster')
        doc = b.fitz.open(output)
        try:
            baseline = pixels(doc, 0)
            doc = click(doc, 0, 'Highlight on this page: Floor finish')
            self.assertNotEqual(baseline, pixels(doc, 0))
            doc = click(doc, 0, 'Highlight on this page: Floor finish')
            self.assertEqual(baseline, pixels(doc, 0))
        finally:
            doc.close()

    def test_selected_overlay_is_excluded_from_print_rendering(self):
        def print_pixels(doc):
            page = b.fitz.mupdf.pdf_page_from_fz_page(doc[0].this)
            pix = b.fitz.mupdf.pdf_new_pixmap_from_page_with_usage(
                page, b.fitz.mupdf.FzMatrix(), b.fitz.csRGB.this, 0, 'Print', b.fitz.mupdf.FZ_CROP_BOX)
            return b.fitz.Pixmap(pix).samples

        doc = b.fitz.open(self.path)
        try:
            original_print = print_pixels(doc)
            original_view = pixels(doc, 0)
            doc = click(doc, 0, 'Highlight on this page: Floor finish')
            self.assertNotEqual(original_view, pixels(doc, 0))
            self.assertEqual(original_print, print_pixels(doc))
        finally:
            doc.close()

    def test_layers_panel_hiding_base_suppresses_its_selection(self):
        doc = b.fitz.open(self.path)
        try:
            doc = click(doc, 0, 'Highlight on this page: Floor finish')
            groups = doc.get_ocgs()
            base = next(x for x, g in groups.items() if g['name'] == 'Takeoff | Floor finish')
            # A viewer panel toggles just the base OCG, without our eye action.
            on = [x for x, g in groups.items() if g['on'] and x != base]
            off = [x for x in groups if x not in on]
            doc.set_layer(-1, on=on, off=off)
            data = doc.tobytes()
            doc.close()
            doc = b.fitz.open(stream=data)
            hidden_pixels = pixels(doc, 0)
            doc = click(doc, 0, 'Clear highlight')
            self.assertEqual(hidden_pixels, pixels(doc, 0))
        finally:
            doc.close()

    def test_separate_legend_selects_the_associated_drawing_page(self):
        folder = Path(self.temp.name) / 'separate'
        manifest, output, _ = fixture(folder, mode='Raster')
        manifest.write_text(manifest.read_text().replace('X="600" Y="560"', 'X="1600" Y="1200"'))
        with contextlib.redirect_stdout(io.StringIO()):
            b.build(str(manifest))
        doc = b.fitz.open(output)
        try:
            self.assertEqual(len(doc), 2)
            before = pixels(doc, 0)
            doc = click(doc, 1, 'Highlight on this page: Trim measurement')
            self.assertNotEqual(before, pixels(doc, 0))
            doc = click(doc, 1, 'Clear highlight')
            self.assertEqual(before, pixels(doc, 0))
        finally:
            doc.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
