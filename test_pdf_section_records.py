# SPDX-License-Identifier: AGPL-3.0-only
import copy
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
from pdf_section_records import normalize_section_records as normalize
import pdf_takeoff_builder as builder


def record(guid='A', qty='1000', share='0.5', **extra):
    return dict(ItemGUID=guid, ParentQty=qty, SectionShare=share,
                ItemName='Trim pieces', ItemType='Count', Color='255', Units='LN FT',
                sections=[{'digitizer_xml': '<Points><Point X="80" Y="180"/></Points>'}], **extra)


def job(*pages):
    return {'ItemLayout': 'SectionRecordsV1', 'pages': [dict(PageGUID=str(i), items=items)
                                                     for i, items in enumerate(pages)]}


class SectionRecordTests(unittest.TestCase):
    def test_parent_calculated_count_qty_is_allocated_on_each_page(self):
        result = normalize(job([record()], [record()]))
        for page in result['pages']:
            self.assertEqual(Decimal(page['items'][0]['PageTotal']), 500)
            self.assertEqual(page['items'][0]['Units'], 'LN FT')
            self.assertEqual(page['items'][0]['PageTotalBasis'], 'Qty')

    def test_merge_retains_order_geometry_and_holes(self):
        first = record('{a}', share='0.25', SymbolType='Circle', hatch_pat='pattern')
        first['sections'][0]['subtracts'] = [{'digitizer_xml': '<Points/>'}]
        second = record('A', share='0.25')
        second['sections'][0]['digitizer_xml'] = '<Points><Point X="150" Y="220"/></Points>'
        source = job([first, record('B'), second], [])
        saved = copy.deepcopy(source)
        result = normalize(source)
        self.assertEqual(source, saved)
        self.assertEqual([i['ItemGUID'] for i in result['pages'][0]['items']], ['{a}', 'B'])
        item = result['pages'][0]['items'][0]
        self.assertEqual(Decimal(item['PageTotal']), 500)
        self.assertEqual(item['sections'], first['sections'] + second['sections'])
        self.assertEqual(item['SymbolType'], 'Circle')
        self.assertEqual(item['hatch_pat'], 'pattern')
        self.assertEqual(result['pages'][1], {'PageGUID': '1', 'items': []})
        self.assertIs(normalize(result), result)

    def test_filtered_share_is_not_scaled_up(self):
        result = normalize(job([record(share='0.25')]))
        self.assertEqual(Decimal(result['pages'][0]['items'][0]['PageTotal']), 250)

    def test_localized_decimal_zero_and_negative(self):
        result = normalize(job([record('A', '0'), record('B', share='0'),
                                record('C', '-10,5', '0,5')]))
        self.assertEqual(len(result['pages'][0]['items']), 1)
        self.assertEqual(Decimal(result['pages'][0]['items'][0]['PageTotal']), Decimal('-5.25'))

    def test_existing_grouped_manifest_is_unchanged(self):
        source = {'pages': [{'items': [{'PageTotal': '123', 'sections': []}]}]}
        self.assertIs(normalize(source), source)

    def test_invalid_numbers_fail_explicitly(self):
        for invalid in ('', 'None', 'nan', 'Infinity', '1,2,3'):
            for field in ('ParentQty', 'SectionShare'):
                with self.subTest(invalid=invalid, field=field):
                    item = record(); item[field] = invalid
                    with self.assertRaises(ValueError): normalize(job([item]))

    def test_identity_and_layout_are_required(self):
        with self.assertRaises(ValueError): normalize(job([record('')]))
        with self.assertRaises(ValueError): normalize({'ItemLayout': 'FutureLayout'})

    def test_mid_export_item_changes_are_rejected(self):
        for field, value in [('ParentQty', '900'), ('Units', 'EA'),
                             ('Color', '0'), ('ItemType', 'Area')]:
            changed = record(); changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalize(job([record(), changed]))

    def test_raw_manifest_renders_like_equivalent_grouped_manifest(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            Image.new('RGB', (1000, 700), 'white').save(folder/'{TEST}.tiff', dpi=(72,72))
            (folder/'Data.xml').write_text('<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{TEST}"/></Item>')
            def section(x):
                return ['--- SECTION_START ---', 'Closed: False', 'DigitizerData:',
                        f'<Points><Point X="{x}" Y="300"/></Points>', '--- SECTION_END ---']
            meta = ['ItemGUID: TRIM', 'ItemName: Trim pieces', 'ItemType: Count', 'Color: 255', 'Units: LN FT']
            for raw in (False, True):
                name = 'raw' if raw else 'grouped'
                lines = ['OutputPDF: '+str(folder/(name+'.pdf')), 'IncludeLegend: True']
                if raw: lines.append('ItemLayout: SectionRecordsV1')
                for number in (1,2):
                    lines += ['--- PAGE_START ---', 'PageGUID: '+str(number), 'PageName: TEST-'+str(number),
                              'PageFolder: '+str(folder), 'PageMultiplier: 1', 'ScaleX: 1', 'ScaleY: 1']
                    if raw:
                        for x in (100,200):
                            lines += ['--- ITEM_START ---']+meta+['ParentQty: 1000', 'SectionShare: 0.25']+section(x)+['--- ITEM_END ---']
                    else:
                        lines += ['--- ITEM_START ---']+meta+['PageTotal: 500', 'PageTotalBasis: Qty']+section(100)+section(200)+['--- ITEM_END ---']
                    lines += ['--- PAGE_END ---']
                manifest = folder/(name+'.txt'); manifest.write_text('\n'.join(lines))
                parsed = builder.parse_manifest(manifest)
                self.assertEqual(len(parsed['pages'][0]['items']), 1)
                self.assertEqual(len(parsed['pages'][0]['items'][0]['sections']), 2)
                builder.build(str(manifest), strict=True)
            with builder.fitz.open(folder/'raw.pdf') as rawdoc, builder.fitz.open(folder/'grouped.pdf') as grouped:
                self.assertEqual(len(rawdoc), 2)
                self.assertEqual(list(rawdoc.get_ocgs().values()), list(grouped.get_ocgs().values()))
                for a,b in zip(rawdoc, grouped):
                    self.assertIn('500 LN FT', a.get_text())
                    self.assertEqual(a.get_text(), b.get_text())
                    self.assertEqual(a.get_pixmap().samples, b.get_pixmap().samples)
                    self.assertEqual(a.annot_xrefs(), b.annot_xrefs())


if __name__ == '__main__':
    unittest.main()
