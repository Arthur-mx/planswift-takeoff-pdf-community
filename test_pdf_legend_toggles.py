# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest

import pdf_takeoff_builder as b
from test_pdf_source_background import fixture


def toggle_links(doc, page):
    result = []
    for xref, kind, _ in page.annot_xrefs():
        if (doc.xref_get_key(xref, 'A/S')[1] == '/SetOCGState'
                and doc.xref_get_key(xref, 'Contents')[1].startswith('Show/hide takeoff:')):
            state = doc.xref_get_key(xref, 'A/State')[1]
            target = int(re.search(r'/Toggle\s+(\d+) 0 R', state).group(1))
            rect = b.fitz.Rect([float(n) for n in doc.xref_get_key(xref, 'Rect')[1].strip('[]').split()])
            result.append((xref, target, rect * page.transformation_matrix))
    return result


class LegendToggleTests(unittest.TestCase):
    def test_saved_actions_target_shared_layer_and_legend_remains_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            for mode in ('PDF', 'Raster'):
                manifest, output, _ = fixture(Path(temp) / mode, mode=mode)
                text = manifest.read_text()
                # The same takeoff on two pages must have one shared layer.
                manifest.write_text(text + '\n' + text[text.index('--- PAGE_START ---'):])
                with contextlib.redirect_stdout(io.StringIO()):
                    b.build(str(manifest))
                with b.fitz.open(output) as doc:
                    links = [toggle_links(doc, page) for page in doc]
                    self.assertEqual([len(rows) for rows in links], [1, 1])
                    target = links[0][0][1]
                    self.assertEqual(target, links[1][0][1])
                    self.assertEqual(doc.get_ocgs()[target]['name'], 'Takeoff | Trim measurement')
                    for page, rows in zip(doc, links):
                        self.assertTrue(page.rect.contains(rows[0][2]))
                        self.assertEqual(doc.xref_get_key(rows[0][0], 'OC')[0], 'null')
                    trace = b.fitz.Rect(178, 278, 302, 283)
                    eye = links[0][0][2]
                    legend_text = b.fitz.Rect(eye.x1 + 1, eye.y0, 780, eye.y1)
                    before = [page.get_pixmap(clip=trace).samples for page in doc]
                    eye_before = doc[0].get_pixmap(clip=eye).samples
                    text_before = doc[0].get_pixmap(clip=legend_text).samples
                    layer = next(c['number'] for c in doc.layer_ui_configs()
                                 if c['text'] == 'Takeoff | Trim measurement')
                    # Toggle the actual OCG targeted by the annotation and test
                    # both pages, the icon state, and the persistent legend row.
                    doc.set_layer_ui_config(layer, action=1)
                    for i, page in enumerate(doc):
                        self.assertNotEqual(before[i], page.get_pixmap(clip=trace).samples)
                    self.assertNotEqual(eye_before, doc[0].get_pixmap(clip=eye).samples)
                    self.assertEqual(text_before, doc[0].get_pixmap(clip=legend_text).samples)
                    self.assertEqual(len(toggle_links(doc, doc[0])), 1)
                    doc.set_layer_ui_config(layer, action=1)
                    self.assertEqual(before[0], doc[0].get_pixmap(clip=trace).samples)
                    self.assertEqual(eye_before, doc[0].get_pixmap(clip=eye).samples)

    def test_rotated_click_target_and_existing_links_are_preserved(self):
        with b.fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.set_rotation(90)
            page.insert_link({'kind': b.fitz.LINK_URI, 'from': b.fitz.Rect(20, 20, 80, 40),
                              'uri': 'https://example.com'})
            oc = doc.add_ocg('Takeoff')
            hit = b.fitz.Rect(100, 100, 125, 130)
            b.draw_legend_layer_toggle(page, hit, hit, page.derotation_matrix, oc, 'Test')
            self.assertEqual(len(page.annot_xrefs()), 2)
            _, target, rect = toggle_links(doc, page)[0]
            self.assertEqual(target, oc)
            self.assertEqual(rect * page.rotation_matrix, hit)

    def test_separate_legend_page_owns_click_target(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, output, _ = fixture(Path(temp), mode='Raster')
            manifest.write_text(manifest.read_text().replace('X="600" Y="560"', 'X="1600" Y="1200"'))
            with contextlib.redirect_stdout(io.StringIO()):
                b.build(str(manifest))
            with b.fitz.open(output) as doc:
                self.assertEqual(len(doc), 2)
                self.assertEqual(toggle_links(doc, doc[0]), [])
                self.assertEqual(len(toggle_links(doc, doc[1])), 1)

    def test_no_legend_means_no_toggle_annotations(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, output, _ = fixture(Path(temp))
            manifest.write_text(manifest.read_text().replace('IncludeLegend: True', 'IncludeLegend: False'))
            with contextlib.redirect_stdout(io.StringIO()):
                b.build(str(manifest))
            with b.fitz.open(output) as doc:
                self.assertEqual(toggle_links(doc, doc[0]), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
