# SPDX-License-Identifier: AGPL-3.0-only
"""Regression coverage for visual defaults, rendered legends and preview regeneration."""
import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import fitz
import pdf_legend_defaults as layout
import pdf_export_picker as picker
import pdf_export_preview as preview
import pdf_takeoff_builder as builder
from test_pdf_source_background import fixture


class LayoutTests(unittest.TestCase):
    def test_validation_rejects_corrupt_settings(self):
        for change in ({'font': float('nan')}, {'font': 0}, {'rect':[0,0,2,1]},
                       {'rect':[0,0,0,1]}, {'rect':'bad'}, {'font':None}):
            self.assertEqual(layout.clean_defaults({'Landscape':dict(layout.default_profile(),**change)}),{})
        self.assertEqual(layout.clean_defaults(None),{})

    def test_relative_area_scales_but_font_is_physical_points(self):
        profile=dict(rect=[.05,.05,.5,.3],font=12,anchor='top')
        for width,height in ((800,600),(2400,1800)):
            font,box=layout.preferred_layout(fitz.Rect(0,0,width,height),None,profile,lambda f:f*20,lambda f:f*4)
            self.assertEqual(font,12)
            self.assertAlmostEqual(box.x0,width*.05)
            self.assertEqual(box.height,48)

    def test_bottom_anchor_and_shrink_to_fit(self):
        profile=dict(rect=[.05,.6,.45,.95],font=20,anchor='bottom')
        font,box=layout.preferred_layout(fitz.Rect(0,0,800,600),None,profile,lambda f:f*25,lambda f:f*5)
        self.assertLess(font,20)
        self.assertEqual(box.y1,570)
        self.assertLessEqual(box.width,320)

    def test_collision_and_overflow_return_automatic_fallback(self):
        profile=dict(rect=[.05,.05,.5,.3],font=12,anchor='top')
        self.assertIsNone(layout.preferred_layout(fitz.Rect(0,0,800,600),fitz.Rect(40,30,90,80),profile,lambda f:50,lambda f:30))
        self.assertIsNone(layout.preferred_layout(fitz.Rect(0,0,800,600),None,profile,lambda f:10000,lambda f:30))

    def test_preferences_survive_unrelated_option_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prefs.json'; config={'Landscape':layout.default_profile()}
            picker.save_preferences(path,replace(picker.ExportPreferences(),legend_defaults=config))
            current=picker.load_preferences(path)
            picker.save_preferences(path,replace(current,preview_view='List'))
            self.assertEqual(picker.load_preferences(path).legend_defaults,config)

    def test_snapshot_remap_keeps_removed_pages_and_history_when_legends_change(self):
        old=['a','a','b','c']; new=['a','b','b','c']
        snapshot=dict(order=[2,0,1],selected=[0,1],current=1)
        self.assertEqual(preview.remap_snapshot(snapshot,old,new),dict(order=[1,2,0],selected=[0],current=0))
        self.assertEqual(preview.remap_snapshot(dict(order=[],selected=[],current=None),old,new),dict(order=[],selected=[],current=None))


class RenderTests(unittest.TestCase):
    def test_default_location_and_font_apply_to_export_and_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); manifest,output,_=fixture(root/'job',mode='Raster')
            config={'Landscape':dict(rect=[.04,.7,.7,.95],font=12,anchor='bottom')}
            with contextlib.redirect_stdout(io.StringIO()):
                details=builder.build(manifest,strict=True,legend_defaults=config,legend_editor_dir=root/'templates')
            template=next(iter(details['legend_templates'].values()))
            self.assertTrue(Path(template['image']).exists())
            self.assertFalse(template['manual'])
            self.assertEqual(template['rows'],[['500 LN FT','Trim measurement']])
            with fitz.open(output) as pdf:
                spans=[s for block in pdf[0].get_text('dict')['blocks'] if 'lines' in block for line in block['lines'] for s in line['spans']]
                title=next(s for s in spans if s['text'].startswith('TAKEOFF SUMMARY'))
                self.assertAlmostEqual(title['size'],12)
                self.assertGreater(title['bbox'][1],420)
                self.assertTrue(any('/SetOCGState' in pdf.xref_object(i) for i in range(1,pdf.xref_length())))
                reference=fitz.Pixmap(template['image'])
                actual=pdf[0].get_pixmap(alpha=False)
                for x in range(177,304):
                    for y in range(276,285):
                        self.assertEqual(reference.pixel(x,y),actual.pixel(x,y))

    def test_direct_export_and_preview_worker_use_saved_defaults(self):
        import os, subprocess, sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); manifest,output,_=fixture(root/'job',mode='Raster')
            original=manifest.read_bytes()
            config={'Landscape':dict(rect=[.04,.7,.7,.95],font=12,anchor='bottom')}
            prefs=root/'PlanSwift'/'Takeoff PDF Community'/'preferences.json'
            picker.save_preferences(prefs,replace(picker.ExportPreferences(),legend_defaults=config))
            env=dict(os.environ,LOCALAPPDATA=str(root))
            subprocess.run([sys.executable,str(Path(builder.__file__).resolve()),str(manifest)],
                           env=env,capture_output=True,check=True,timeout=30)
            with fitz.open(output) as doc:
                self.assertGreater(doc[0].search_for('TAKEOFF SUMMARY')[0].y0,420)
            manifest.write_bytes(original)
            work=root/'worker'; work.mkdir()
            subprocess.run([sys.executable,str(Path(preview.__file__).resolve()),'--render',str(manifest),
                            str(work),'--preferences',str(prefs)],capture_output=True,check=True,timeout=30)
            with fitz.open(work/'preview.pdf') as doc:
                self.assertGreater(doc[0].search_for('TAKEOFF SUMMARY')[0].y0,420)
            self.assertTrue(json.loads((work/'ready.json').read_text())['legend_templates'])

    def test_planswift_box_wins_and_collision_uses_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for manual in (False,True):
                folder=root/str(manual); manifest,output,_=fixture(folder,mode='Raster')
                if manual:
                    text=manifest.read_text().replace('PageMultiplier: 2','PageMultiplier: 2\nLegendData:\n<Points><Point X="900" Y="40"/><Point X="1500" Y="300"/></Points>\nLegendDataEnd')
                    manifest.write_text(text)
                config={'Landscape':dict(rect=[.1,.45,.9,.85],font=12,anchor='top')}
                with contextlib.redirect_stdout(io.StringIO()): details=builder.build(manifest,strict=True,legend_defaults=config)
                if not manual: self.assertTrue(any('saved legend area' in w for w in details['warnings']))
                else: self.assertFalse(any('saved legend area' in w for w in details['warnings']))
                with fitz.open(output) as pdf:
                    title=pdf[0].search_for('TAKEOFF SUMMARY')[0]
                    self.assertGreater(title.x0,400 if manual else 312)


class EditorTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root=tk.Tk(); self.root.attributes('-alpha',0); self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(args)
        self.saved=[]
        self.dialog=layout.LegendDefaultsDialog(self.root,{},self.saved.append)
        self.dialog.window.attributes('-alpha',0)
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors,[])

    def test_corner_drag_resize_save_and_orientation(self):
        d=self.dialog; d.snap('Bottom left'); d.font.set('14'); d.redraw()
        x,y,_,_=d.area
        d.start_drag(SimpleNamespace(x=x+15,y=y+15)); d.drag(SimpleNamespace(x=x+45,y=y+25))
        self.assertGreater(d.rect[0],.025)
        x,y=d.area[2:]; width=d.rect[2]-d.rect[0]
        d.start_drag(SimpleNamespace(x=x,y=y)); d.drag(SimpleNamespace(x=x+30,y=y))
        self.assertGreater(d.rect[2]-d.rect[0],width)
        d.orientation.set('Portrait'); d.switch_orientation(); d.snap('Top right')
        d.save()
        self.assertEqual(set(self.saved[0]),{'Landscape','Portrait'})
        self.assertEqual(self.saved[0]['Landscape']['font'],14)
        self.assertEqual(self.saved[0]['Landscape']['anchor'],'bottom')

    def test_cancel_and_invalid_font_do_not_save(self):
        d=self.dialog; d.enabled.set(True); d.font.set('nan'); d.save()
        self.assertFalse(self.saved); self.assertTrue(d.window.winfo_exists())
        d.window.destroy(); self.assertFalse(self.saved)

    def test_zoom_drag_coordinates_and_reset(self):
        d=self.dialog; d.snap('Top left'); d.zoom.set('200%'); d.redraw()
        d.canvas.xview_moveto(.02); d.canvas.yview_moveto(.02)
        old=list(d.rect)
        ex=old[0]*d.mapping[2]+30-d.canvas.canvasx(0)
        ey=old[1]*d.mapping[3]+30-d.canvas.canvasy(0)
        d.start_drag(SimpleNamespace(x=ex,y=ey)); d.drag(SimpleNamespace(x=ex+100,y=ey+100))
        self.assertGreater(d.rect[0],old[0])
        d.reset(); self.assertFalse(d.enabled.get())


class RebuildTests(unittest.TestCase):
    def test_edit_on_real_page_preserves_order_removal_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); manifest,_,_=fixture(root/'job',mode='Raster')
            text=manifest.read_text(); page=text[text.index('--- PAGE_START ---'):]
            manifest.write_text(text+'\n'+page.replace('A-101','A-102'))
            prefs=root/'prefs.json'
            ui=preview.PreviewWindow(manifest,start=False,preferences_path=prefs)
            ui.root.attributes('-alpha',0); ui.root.update()
            try:
                with contextlib.redirect_stdout(io.StringIO()): preview.render_preview(manifest,ui.workdir,prefs)
                ui.load_document(); ui.root.update()
                keys=list(ui.details['page_keys']); ui.apply_drawing_order([keys[1]])
                before_undo=len(ui.undo_stack)
                with patch.object(ui,'start_build'):
                    dialog=ui.edit_legend_defaults(); dialog.window.attributes('-alpha',0)
                    self.assertTrue(dialog.template['image'])
                    dialog.snap('Bottom left'); dialog.rect=[.04,.7,.7,.95]; dialog.save()
                self.assertIsNone(ui.document)
                with contextlib.redirect_stdout(io.StringIO()): preview.render_preview(manifest,ui.workdir,prefs)
                ui.load_document(); ui.root.update()
                self.assertEqual(ui.drawing_order(),[keys[1]])
                self.assertEqual(len(ui.undo_stack),before_undo)
                ui.undo_order(); self.assertEqual(ui.drawing_order(),keys)
            finally: ui.close()


if __name__=='__main__': unittest.main()
