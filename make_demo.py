# SPDX-License-Identifier: AGPL-3.0-only
"""Generate an entirely fictional, interactive sample using the real PDF builder.
Run after installing requirements-dev.txt. Does not use a PlanSwift job.
"""
from pathlib import Path
import json
import tempfile
import fitz
from PIL import Image
import pdf_takeoff_builder as builder


def main():
    root = Path(__file__).resolve().parent
    destination = root / 'examples' / 'Interactive Demo.pdf'
    destination.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='TakeoffPDFDemo_') as temp:
        work = Path(temp)
        sources = work / 'Plans & Docs' / 'Demo'
        sources.mkdir(parents=True)
        lines = ['ItemLayout: SectionRecordsV1', 'OutputPDF: ' + str(destination), 'BackgroundMode: PDF',
                 'SourceRootJSON: ' + json.dumps(str(work)), 'IncludeLegend: True',
                 'Greyscale: False', 'ProjectName: Synthetic community demonstration']
        for kind in ('Area','Linear','Count','WallArea'):
            filename = {'Area':'area','Linear':'linear','Count':'count','WallArea':'wall'}[kind]
            lines.append('LegendIcon' + kind + ': ' + str(root/'assets'/f'{filename}.png'))
        for number in (1, 2):
            sheet = f'DEMO-{number:02}'
            storage = work / sheet
            storage.mkdir()
            with fitz.open() as source:
                page = source.new_page(width=1200, height=800)
                page.insert_text((45, 95), 'TAKEOFF PDF COMMUNITY', fontsize=28, color=(.08,.2,.3))
                page.insert_text((45, 120), 'Synthetic example - not a construction drawing', fontsize=13, color=(.35,.4,.45))
                page.insert_text((45, 178), 'Try the legend eye controls, then click an item description.', fontsize=14)
                page.insert_text((45, 204), 'Use a desktop PDF reader that supports layer actions.', fontsize=12)
                page.draw_rect(fitz.Rect(40, 290, 790, 725), color=(.7,.75,.8), width=1)
                page.insert_text((60, 320), 'FLOOR FINISH / BASE / FIXTURES' if number==1 else 'SECOND DRAWING - SAME TAKEOFF LAYERS', fontsize=15)
                for x,y,w,h,label in ((90,390,240,270,'ROOM A'),(360,390,280,270,'ROOM B')):
                    page.draw_rect(fitz.Rect(x,y,x+w,y+h),color=(.22,.25,.3),width=3)
                    page.insert_text((x+70,y+145),label,fontsize=18,color=(.22,.25,.3))
                page.insert_text((45, 770), sheet + ' | Fictional geometry and quantities for demonstration only', fontsize=12)
                source.save(sources/f'{sheet}.pdf')
                pix=page.get_pixmap()
                Image.frombytes('RGB',(pix.width,pix.height),pix.samples).save(storage/'{DEMO}.tiff',dpi=(72,72))
            (storage/'Data.xml').write_text('<Item><Property Name="Image" ImageType="TTiffGraphic" GUID="{DEMO}"/></Item>')
            lines += ['--- PAGE_START ---','PageGUID: '+sheet,'PageName: '+sheet,
                      'PageFolder: '+str(storage),r'TakeoffPath: ..\Pages\Plans & Docs\Demo',
                      'ScaleX: 10','ScaleY: 10','PageMultiplier: 1']
            entries=[('FLOOR','Floor finish - resilient flooring','Area','11769395','420','SQ FT',[(90,390),(330,390),(330,660),(90,660)]),
                     ('BASE','Wall base - rubber cove base','Linear','16744448','146','LN FT',[(360,390),(640,390),(640,660),(360,660)]),
                     ('FIXTURES','Wall fixtures - count','Count','255','4','EA',[(370,410),(620,410),(370,640),(620,640)])]
            for guid,name,kind,color,qty,unit,points in entries:
                lines += ['--- ITEM_START ---','ItemGUID: '+guid,'ItemName: '+name,
                          'ItemType: '+kind,'Color: '+color,'ParentQty: '+str(int(qty)*2),
                          'SectionShare: 0.5','Units: '+unit,'--- SECTION_START ---',
                          'Closed: '+str(kind=='Area'),'DigitizerData:',
                          '<Points>'+''.join(f'<Point X="{x}" Y="{y}"/>' for x,y in points)+'</Points>',
                          '--- SECTION_END ---','--- ITEM_END ---']
            lines += ['--- PAGE_END ---']
        manifest=work/'demo.manifest.txt'
        manifest.write_text('\n'.join(lines),encoding='utf-8')
        builder.build(str(manifest),strict=True)
    print(destination)
    return destination


if __name__ == '__main__':
    main()
