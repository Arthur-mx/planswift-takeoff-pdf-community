# SPDX-License-Identifier: AGPL-3.0-only
import copy
import pdf_takeoff_builder as b

def render_sample(folder):
    from PIL import Image,ImageDraw
    folder.mkdir(parents=True,exist_ok=True)
    image=Image.new('RGB',(1100,700),'white'); d=ImageDraw.Draw(image)
    d.rectangle((40,90,530,640),outline='gray',width=3)
    image.save(folder/'{TEST}.tiff',dpi=(72,72))
    (folder/'Data.xml').write_text('<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{TEST}"/></Item>')
    job={'pages':[{'items':[{'ItemGUID': str(i), 'ItemName': 'Original item '+str(i), 'ItemType': 'Linear', 'Color': c} for i,c in enumerate(('255','65280','16711680','65535','0'))]} for _ in range(2)]}; expected=copy.deepcopy(job)
    for page in job['pages']:
        page['items'][0]['ItemName']='Original item - retains PlanSwift red'
    lines=['OutputPDF: '+str(folder/'export_color_test.pdf'),'IncludeLegend: True','Greyscale: True']
    for page_idx,page in enumerate(job['pages']):
        lines+=['--- PAGE_START ---','PageName: TEST-'+str(page_idx+1),'PageFolder: '+str(folder),
                'ScaleX: 1','ScaleY: 1','PageMultiplier: 1',
                'LegendData:','<Points><Point X="580" Y="110"/><Point X="1050" Y="500"/></Points>','LegendDataEnd']
        for index,item in enumerate(page['items']):
            lines+=['--- ITEM_START ---']+[k+': '+v for k,v in item.items()]
            y=150+90*index
            lines+=['PageTotal: 400','Units: LF','--- SECTION_START ---','Closed: False',
                    'DigitizerData:',f'<Points><Point X="80" Y="{y}"/><Point X="480" Y="{y}"/></Points>',
                    '--- SECTION_END ---','--- ITEM_END ---']
        lines+=['--- PAGE_END ---']
    manifest=folder/'sample_manifest.txt'; manifest.write_text('\n'.join(lines),encoding='utf-8')
    b.build(str(manifest))
    doc=b.fitz.open(folder/'export_color_test.pdf')
    assert len(doc)==2
    for page in doc:
        assert 'TAKEOFF SUMMARY' in page.get_text()
        drawings=page.get_drawings()
        for item in expected['pages'][0]['items']:
            rgb=b.tcolor_to_rgb01(b.pdf_item_color(item))
            match=lambda c: c is not None and all(abs(x-y)<1e-5 for x,y in zip(c,rgb))
            assert any(match(v['color']) and abs(v['width']-b.LINEAR_WIDTH_PT)<.01 for v in drawings)
            assert any(match(v['fill']) for v in drawings),('Missing legend color',item)
    doc[0].get_pixmap(matrix=b.fitz.Matrix(1,1)).save(folder/'export_color_test.png')
    print('Rendered two-page sample: expected geometry and legend colors verified on both pages.')

