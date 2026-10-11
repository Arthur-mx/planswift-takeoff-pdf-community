# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
from PIL import Image
import pdf_page_annotations as a
import pdf_takeoff_builder as b


def xml(points):
    return '<Points>' + ''.join(f'<Point X="{x}" Y="{y}"/>' for x, y in points) + '</Points>'


def record(kind='RubberStamp', points=((100, 120), (350, 100)), **properties):
    return dict(NativeType=kind, digitizer_xml=xml(points), PrimaryLabel='APPROVED',
                Transparency='200', **properties)


class NativeAnnotationTests(unittest.TestCase):
    def render(self, records, rotation=0, include_hidden=False):
        doc = fitz.open()
        page = doc.new_page(width=600, height=600)
        page.set_rotation(rotation)
        layers = {}
        bounds, warnings = a.render_page_annotations(page, records, fitz.Point,
                                                     page.derotation_matrix, layers,
                                                     b.tcolor_to_rgb01, include_hidden)
        self.addCleanup(doc.close)
        return doc, page, bounds, warnings

    def test_every_page_item_category_has_independent_visibility(self):
        for category in ('notes', 'highlighters', 'dimensions', 'annotations'):
            visible = {'Visible': 'True'}
            hidden = {'Visible': 'False'}
            page = {category: [visible, hidden]}
            self.assertEqual(b.selected_page_items(page, category, {'PageItems':'NONE'}), [])
            self.assertEqual(b.selected_page_items(page, category, {'PageItems':'VISIBLE'}), [visible])
            self.assertEqual(b.selected_page_items(page, category, {'PageItems':'ALL'}), [visible,hidden])

    def test_page_item_mode_controls_real_export_with_no_takeoff(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            Image.new('RGB',(600,600),'white').save(folder/'{PAGE}.tiff',dpi=(72,72))
            (folder/'Data.xml').write_text('<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{PAGE}"/></Item>')
            for mode, expected in [('VISIBLE',1), ('ALL',2), ('NONE',0)]:
                output=folder/(mode+'.pdf')
                lines=['OutputPDF: '+str(output),'PageItems: '+mode,'--- PAGE_START ---',
                       'PageName: A','PageFolder: '+str(folder)]
                for visible,y in [('True',100),('False',300)]:
                    r=record(points=((100,y),(350,y)), Visible=visible)
                    lines+=['--- ANNOTATION_START ---']+[k+': '+v for k,v in r.items() if k!='digitizer_xml']
                    lines+=['DigitizerData:',r['digitizer_xml'],'--- ANNOTATION_END ---']
                lines+=['--- PAGE_END ---']
                path=folder/'manifest.txt'; path.write_text('\n'.join(lines))
                with contextlib.redirect_stdout(io.StringIO()):
                    b.build(str(path))
                with fitz.open(output) as doc:
                    self.assertEqual(doc[0].get_text().count('APPROVED'), expected)
                    self.assertEqual(len(doc.get_ocgs()), int(expected>0))

    def test_cloud_cusps_match_native_triangle(self):
        raw = [fitz.Point(104.34375,286.119018554688),
               fitz.Point(1074.5986328125,183.191696166992),
               fitz.Point(231.816711425781,1203.96032714844)]
        curves = a.cloud_curves(raw)
        self.assertEqual(len(curves), 36)  # 18 semicircles, two cubics each.
        self.assertLess(abs(curves[0][0] - fitz.Point(168.0802,745.0397)), .001)
        self.assertLess(abs(curves[2][0] - fitz.Point(163.5310,603.3849)), 2)
        self.assertLess(abs(curves[-1][-1] - curves[0][0]), 1e-6)

    def test_cloud_winding_does_not_invert_lobes(self):
        raw = [fitz.Point(100,100), fitz.Point(400,100), fitz.Point(300,400)]
        forward = a.cloud_curves(raw)
        backward = a.cloud_curves(list(reversed(raw)))
        def coords(curves):
            return sorted((round(p.x,5), round(p.y,5)) for curve in curves for p in curve)
        self.assertEqual(coords(forward), coords(backward))
        self.assertEqual(coords(forward), coords(a.cloud_curves(raw + raw[:1])))

    def test_cloud_is_outline_only_at_every_transparency(self):
        for alpha in ('0', '100', '200', '255'):
            cloud = record('RevisionCloud', ((100,100),(400,100),(300,400)))
            cloud['Transparency'] = alpha
            doc, page, _, warnings = self.render([cloud])
            self.assertEqual(warnings, [])
            paths = page.get_drawings()
            self.assertTrue(paths)
            self.assertTrue(all(path['type'] == 's' and path['fill'] is None for path in paths))

    def test_one_stamp_for_two_points_searchable_and_layered(self):
        doc, page, bounds, warnings = self.render([record(LabelColor='32768')])
        self.assertEqual(page.get_text().strip(), 'APPROVED')
        self.assertEqual(sum(d['type'] == 'f' for d in page.get_drawings()), 1)
        self.assertEqual(sum(d['type'] == 's' for d in page.get_drawings()), 1)
        self.assertEqual(len(bounds), 1)
        self.assertEqual([v['name'] for v in doc.get_ocgs().values()], ['Rubber Stamps'])
        self.assertEqual(warnings, [])
        line = page.get_text('dict')['blocks'][0]['lines'][0]
        self.assertLess(line['dir'][1], 0)

    def test_bad_geometry_reports_and_does_not_create_phantom_layers(self):
        cases = [record(points=((1,2),)), record(points=((1,2),(1,2))),
                 record(points=((1,2),(3,4),(5,6))), record(points=((float('nan'),2),(3,4))),
                 record('RevisionCloud', ((1,2),(3,4),(5,6)))]
        broken = record()
        broken['digitizer_xml'] = '<Points>'
        doc, page, bounds, warnings = self.render(cases + [broken])
        self.assertEqual(len(warnings), 6)
        self.assertEqual(bounds, [])
        self.assertEqual(doc.get_ocgs(), {})
        self.assertEqual(page.get_text(), '')

    def test_visibility_and_all_items(self):
        doc, page, bounds, warnings = self.render([record(Visible='False')])
        self.assertEqual(bounds, [])
        doc, page, bounds, warnings = self.render([record(Visible='False')], include_hidden=True)
        self.assertIn('APPROVED', page.get_text())

    def test_rotated_pages_preserve_display_placement(self):
        for rotation in (0,90,180,270):
            doc, page, bounds, warnings = self.render([record()], rotation)
            self.assertIn('APPROVED', page.get_text())
            displayed = page.get_drawings()[0]['rect'] * page.rotation_matrix
            self.assertAlmostEqual(displayed.x0, 97.2483, delta=.001)
            self.assertAlmostEqual(displayed.x1, 352.7517, delta=.001)
            self.assertTrue(bounds[0].contains(page.get_drawings()[0]['rect']))

    def test_tab_first_point_is_tip_and_notes_are_not_printed(self):
        r = record('StickyTab', ((450,110),(100,170)), StoredNotes='PRIVATE HIDDEN NOTE')
        r['PrimaryLabel'] = 'URGENT'
        doc, page, bounds, warnings = self.render([r])
        self.assertEqual(page.get_text().strip(), 'URGENT')
        paths = page.get_drawings()
        vertices = [p for item in paths[0]['items'] for p in item[1:] if isinstance(p,fitz.Point)]
        self.assertIn(fitz.Point(450,110), vertices)
        self.assertEqual(len(paths), 2)
        self.assertEqual([v['name'] for v in doc.get_ocgs().values()], ['Sticky Tabs'])

    def test_timestamp_switch_and_cloud_bounds(self):
        r = record('RevisionCloud', ((100,100),(400,100),(300,400)),
                   ShowTimestamp='True', TimeStampStr='10/11/2026 - 12:49 PM')
        doc, page, bounds, warnings = self.render([r])
        self.assertIn('12:49 PM',page.get_text())
        for drawing in page.get_drawings():
            self.assertTrue(bounds[0].contains(drawing['rect']))
        r['ShowTimestamp']='False'
        doc,page,_,_=self.render([r])
        self.assertEqual(page.get_text(),'')

    def test_stamp_style_switches_and_multiline_label(self):
        r = record(Bordered='False', ShowTimestamp='True',TimeStampStr='10/11/2026')
        r['PrimaryLabel']='APPROVED[[NL]]AS NOTED'
        doc,page,_,warnings=self.render([r])
        self.assertIn('AS NOTED',page.get_text())
        self.assertIn('10/11/2026',page.get_text())
        self.assertEqual(page.get_drawings()[0]['type'],'f')
        self.assertTrue(any('timestamp' in w for w in warnings))

    def test_font_fallback_is_visible_in_report(self):
        with patch.object(a,'label_font',return_value=(fitz.Font('hebo'),True)):
            doc,page,_,warnings=self.render([record()])
        self.assertIn('APPROVED',page.get_text())
        self.assertTrue(any('font unavailable' in w for w in warnings))

    def test_manifest_separates_annotation_data_from_notes_and_items(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'manifest.txt'
            path.write_text('\n'.join(['AnnotationVisibility: All','--- PAGE_START ---',
                'PageName: A','--- NOTE_START ---','Text: note','DigitizerData:',xml([(1,2),(3,4)]),
                '--- NOTE_END ---','--- ANNOTATION_START ---','NativeType: RubberStamp',
                'PrimaryLabel: APPROVED','DigitizerData:',xml([(100,120),(350,100)]),
                '--- ANNOTATION_END ---','--- ITEM_START ---','ItemName: wall',
                '--- ITEM_END ---','--- PAGE_END ---','--- PAGE_START ---','PageName: B','--- PAGE_END ---']))
            job=b.parse_manifest(path)
        self.assertEqual(job['AnnotationVisibility'],'All')
        first,second=job['pages']
        self.assertEqual(len(first['annotations']),1)
        self.assertEqual(first['notes'][0]['Text'],'note')
        self.assertEqual(first['items'][0]['ItemName'],'wall')
        self.assertEqual(first['annotations'][0]['digitizer_xml'],xml([(100,120),(350,100)]))
        self.assertNotIn('annotations',second)

    def test_full_export_annotations_only_and_separate_layers(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            Image.new('RGB',(600,600),'white').save(folder/'{PAGE}.tiff',dpi=(72,72))
            (folder/'Data.xml').write_text('<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{PAGE}"/></Item>')
            output=folder/'out.pdf'
            lines=['OutputPDF: '+str(output),'IncludeLegend: True','--- PAGE_START ---',
                   'PageName: A','PageFolder: '+str(folder)]
            for r in [record(), record('RevisionCloud',((50,300),(300,300),(250,500))),
                      record('StickyTab',((550,200),(350,230)))]:
                lines+=['--- ANNOTATION_START ---']+[k+': '+v for k,v in r.items() if k!='digitizer_xml']
                lines+=['DigitizerData:',r['digitizer_xml'],'--- ANNOTATION_END ---']
            lines+=['--- PAGE_END ---']
            path=folder/'manifest.txt'
            path.write_text('\n'.join(lines))
            with contextlib.redirect_stdout(io.StringIO()):
                b.build(str(path))
            with fitz.open(output) as doc:
                self.assertEqual(len(doc),1)
                self.assertEqual(len(doc.get_ocgs()),3)
                self.assertNotIn('TAKEOFF SUMMARY',doc[0].get_text())
                self.assertNotIn('PAGE MULTIPLIER',doc[0].get_text())
                self.assertEqual(doc[0].get_text().count('APPROVED'),2)


if __name__=='__main__':
    unittest.main()
