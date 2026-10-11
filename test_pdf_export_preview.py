# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import fitz
import pdf_export_preview as preview
import pdf_export_picker as picker
from test_pdf_export_picker import catalog
from test_pdf_source_background import fixture


class PreviewBuildTests(unittest.TestCase):
    def test_finished_export_is_isolated_and_saved_without_losing_interactivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, original, sources = fixture(root / 'project')
            original.write_bytes(b'previous delivery')
            manifest.write_text('SourceRootJSON: ' + json.dumps(str(root / 'project')) + '\n'
                                + manifest.read_text(encoding='utf-8'), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                result = preview.render_preview(manifest, root / 'private')
            self.assertEqual(original.read_bytes(), b'previous delivery')
            self.assertTrue(manifest.exists())
            self.assertEqual(result['page_titles'], ['A-101'])
            pdf = root / 'private' / 'preview.pdf'
            with fitz.open(pdf) as doc:
                self.assertIn('500 LN FT', doc[0].get_text())
                self.assertIn('PAGE MULTIPLIER: 2', doc[0].get_text())
                self.assertTrue(doc.get_ocgs())
                self.assertTrue(any('/SetOCGState' in doc.xref_object(xref)
                                    for xref in range(1, doc.xref_length())))
            delivered = preview.save_preview(pdf, root / 'final.pdf', result['protected_sources'])
            self.assertEqual(delivered.read_bytes(), pdf.read_bytes())
            source = sources / 'A-101.pdf'
            before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, 'original source PDF'):
                preview.save_preview(pdf, source, result['protected_sources'])
            self.assertEqual(source.read_bytes(), before)

    def test_failed_save_keeps_existing_file_and_cleans_pending_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / 'preview.pdf', root / 'existing.pdf'
            source.write_bytes(b'preview')
            target.write_bytes(b'previous export')
            with patch.object(preview.os, 'replace', side_effect=PermissionError('PDF is open')):
                with self.assertRaises(PermissionError):
                    preview.save_preview(source, target)
            self.assertEqual(target.read_bytes(), b'previous export')
            self.assertEqual(set(root.iterdir()), {source, target})

    def test_missing_raster_stops_preview_without_partial_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output, _ = fixture(root / 'project', mode='Raster')
            (manifest.parent / '{PAGE}.tiff').unlink()
            with self.assertRaisesRegex(ValueError, 'Preview stopped'):
                preview.render_preview(manifest, root / 'private')
            self.assertFalse(output.exists())
            self.assertFalse((root / 'private' / 'preview.pdf').exists())

    def test_page_order_and_separate_legend_page_have_matching_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = fixture(root / 'project', mode='Raster')
            text = manifest.read_text(encoding='utf-8').replace('X="600"', 'X="1600"')
            text = text.replace('ItemName: Trim measurement', 'ItemName: Trim ' + ('long description ' * 35))
            # Tracing reaches the sheet edge and exceeds available 12pt width:
            # this must generate an extra legend page, followed by drawing 2.
            page = text[text.index('--- PAGE_START ---'):]
            text += '\n' + page.replace('PageName: A-101', 'PageName: A-102')
            manifest.write_text(text, encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                details = preview.render_preview(manifest, root / 'private')
            self.assertEqual(details['page_titles'], ['A-101', 'A-101 - Legend', 'A-102', 'A-102 - Legend'])
            with fitz.open(root / 'private' / 'preview.pdf') as doc:
                self.assertEqual(len(doc), 4)
                self.assertIn('TAKEOFF SUMMARY - A-102', doc[3].get_text())

    def test_reordered_save_keeps_layers_actions_and_page_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = fixture(root / 'project', mode='Raster')
            text = manifest.read_text(encoding='utf-8')
            page = text[text.index('--- PAGE_START ---'):]
            text += '\n' + page.replace('PageName: A-101', 'PageName: A-102')
            manifest.write_text(text, encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                preview.render_preview(manifest, root / 'private')
            pdf = root / 'private' / 'preview.pdf'
            with fitz.open(pdf) as doc:
                ocgs = doc.get_ocgs()
                xrefs = [doc[i].xref for i in range(len(doc))]
                actions = {i: doc.xref_object(i) for i in range(1, doc.xref_length())
                           if '/SetOCGState' in doc.xref_object(i)}
            destination = preview.save_preview(pdf, root / 'sorted.pdf', order=[1, 0])
            with fitz.open(destination) as doc:
                self.assertIn('TAKEOFF SUMMARY - A-102', doc[0].get_text())
                self.assertEqual([doc[i].xref for i in range(2)], list(reversed(xrefs)))
                self.assertEqual(doc.get_ocgs(), ocgs)
                for xref, value in actions.items():
                    self.assertEqual(doc.xref_object(xref), value)
            before = destination.read_bytes()
            with self.assertRaises(ValueError):
                preview.save_preview(pdf, destination, order=[1, 1])
            self.assertEqual(destination.read_bytes(), before)

    def test_subset_save_preserves_retained_layers_links_and_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _, _ = fixture(root / 'project', mode='Raster')
            text = manifest.read_text(encoding='utf-8')
            page = text[text.index('--- PAGE_START ---'):]
            text += '\n' + page.replace('PageName: A-101', 'PageName: A-102')
            manifest.write_text(text, encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                preview.render_preview(manifest, root / 'private')
            pdf = root / 'private' / 'preview.pdf'
            original = pdf.read_bytes()
            with fitz.open(pdf) as doc:
                page_xref = doc[1].xref
                ocgs = doc.get_ocgs()
                links = {
                    xref: doc.xref_object(xref) for xref, kind, _ in doc[1].annot_xrefs()
                    if '/SetOCGState' in doc.xref_object(xref)}
                self.assertTrue(links)
            destination = preview.save_preview(pdf, root / 'subset.pdf', order=[1])
            with fitz.open(destination) as doc:
                self.assertEqual(len(doc), 1)
                self.assertEqual(doc[0].xref, page_xref)
                self.assertIn('TAKEOFF SUMMARY - A-102', doc[0].get_text())
                self.assertEqual(doc.get_ocgs(), ocgs)
                for xref, value in links.items():
                    self.assertEqual(doc.xref_object(xref), value)
                self.assertTrue(doc[0].get_pixmap().samples)
            self.assertEqual(pdf.read_bytes(), original)
            saved = destination.read_bytes()
            for invalid in ([], [2], [-1], [True], [0, 0]):
                with self.subTest(order=invalid), self.assertRaises(ValueError):
                    preview.save_preview(pdf, destination, order=invalid)
                self.assertEqual(destination.read_bytes(), saved)
            self.assertFalse(list(root.glob('.takeoff-*')))

    def test_preview_protocol_keeps_source_and_requires_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'choice.txt'
            model = picker.SelectionModel(catalog())
            picker.write_result(path, model, True, False, 'PDF', 'PREVIEW', r'C:\Prøyecto\Plans')
            lines = path.read_text(encoding='ascii').splitlines()
            self.assertEqual(lines[6], 'PREVIEW')
            self.assertEqual(json.loads(lines[7]), r'C:\Prøyecto\Plans')
            model.choose(model.pages, False)
            with self.assertRaises(ValueError):
                picker.write_result(path, model, True, False, 'RASTER', 'PREVIEW')


class PreviewWidgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp.name)
        self.manifest, _, _ = fixture(self.root_path / 'project', mode='Raster')
        self.preferences_path = self.root_path / 'preferences.json'
        self.ui = preview.PreviewWindow(self.manifest, start=False, preferences_path=self.preferences_path)
        self.ui.root.attributes('-alpha', 0)
        self.ui.root.geometry('760x520+0+0')
        self.errors = []
        self.ui.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.ui.root.update()

    def tearDown(self):
        self.ui.close()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def test_render_navigation_zoom_save_cancel_and_cleanup(self):
        ui = self.ui
        with fitz.open() as doc:
            doc.new_page(width=800, height=600).insert_text((20, 20), 'First page')
            doc.new_page(width=600, height=800).insert_text((20, 20), 'Second page')
            doc.save(ui.pdf)
        (ui.workdir / 'ready.json').write_text(json.dumps({'page_titles': ['A-2', 'A-1'], 'warnings': []}))
        ui.load_document()
        ui.root.update()
        ui.render_page()
        self.assertIsNotNone(ui.photo)
        self.assertLessEqual(ui.photo.width(), ui.canvas.winfo_width())
        ui.navigate(1)
        ui.zoom.set('150%')
        ui.render_page()
        self.assertEqual(ui.page_number, 1)
        self.assertIn('disabled', ui.next_button.state())
        ui.page_list.selection_clear(0, 'end')
        ui.page_list.selection_set(0)
        ui.select_page()
        self.assertEqual(ui.page_number, 0)
        with patch('tkinter.filedialog.asksaveasfilename', return_value=''):
            ui.save()
        self.assertEqual(list(self.root_path.glob('*.pdf')), [])
        destination = self.root_path / 'saved.pdf'
        with patch('tkinter.filedialog.asksaveasfilename', return_value=str(destination)):
            ui.save()
        self.assertEqual(destination.read_bytes(), ui.pdf.read_bytes())
        workdir = ui.workdir
        ui.close()
        self.assertFalse(workdir.exists())
        self.assertTrue(self.manifest.exists())  # User inputs are never deleted.

    def test_reorder_groups_restore_default_and_return_to_print_window(self):
        from test_pdf_export_picker import catalog
        state = self.root_path / 'session.json'
        prefs = self.root_path / 'preferences.json'
        form = picker.PickerWindow(catalog(), self.root_path / 'choice.txt', prefs, state_path=state)
        form.root.withdraw()
        form.model.order = [6, 4, 2]
        form.model.checked = {6, 4, 2}
        form.model.custom_order = form.model.edited = True
        form.mode.set('ALL')
        form.legend.set(False)
        form.greyscale.set(False)
        form.source_root.set(self.temp.name)
        form.search.set('October')
        form.save_session()
        form.close()
        ui = self.ui
        ui.root.geometry('1000x900+0+0')
        ui.state_path = state
        with fitz.open() as doc:
            for title in ('D', 'D legend', 'C', 'A'):
                doc.new_page().insert_text((20, 20), title)
            doc.save(ui.pdf)
        (ui.workdir / 'ready.json').write_text(json.dumps({
            'page_titles': ['D', 'D legend', 'C', 'A'], 'page_keys': ['d', 'd', 'c', 'a']}))
        ui.load_document()
        ui.root.update()
        ui.page_list.see(3)
        last = ui.page_list.bbox(3)
        ui.start_drag(type('Drag', (), {'y': last[1] + 10})())
        ui.page_list.see(1)
        legend = ui.page_list.bbox(1)
        ui.drag_motion(type('Drag', (), {'y': legend[1] + 40})())
        ui.root.update_idletasks()
        self.assertEqual(ui.drag_position, 0)  # Before the whole drawing/legend group.
        self.assertEqual(ui.drop_line.winfo_y(), max(1, ui.page_list.bbox(0)[1]))
        ui.finish_drag(type('Drag', (), {'x': -10, 'y': legend[1] + 40})())
        self.assertFalse(ui.drop_line.winfo_ismapped())
        self.assertEqual(ui.page_order, [0, 1, 2, 3])  # Releasing outside cancels.
        ui.show_page(1)  # Selecting the companion legend moves its drawing too.
        ui.move_drawing('bottom')
        self.assertEqual(ui.page_order, [2, 3, 0, 1])
        ui.restore_order()
        self.assertEqual(ui.page_order, [3, 2, 0, 1])
        ui.show_page(0)
        ui.move_drawing('down')
        self.assertEqual(ui.page_order, [2, 3, 0, 1])
        ui.back_to_print()
        self.assertEqual(ui.return_code, 3)
        self.assertTrue(ui.closed)
        resumed = picker.PickerWindow(catalog(), self.root_path / 'choice.txt', prefs, state_path=state)
        resumed.root.withdraw()
        try:
            self.assertEqual(resumed.model.selected_guids(), ['c', 'a', 'd'])
            self.assertEqual(resumed.mode.get(), 'ALL')
            self.assertFalse(resumed.legend.get())
            self.assertFalse(resumed.greyscale.get())
            self.assertEqual(resumed.source_root.get(), self.temp.name)
            self.assertEqual(resumed.search.get(), 'October')
            # The next preview launch must keep the same order and options.
            resumed.save_session()
            self.assertEqual(picker.read_session_state(state)['order'], ['c', 'a', 'd'])
        finally:
            resumed.close()

    def test_back_during_build_cancels_worker_and_keeps_existing_choices(self):
        ui = self.ui
        ui.state_path = self.root_path / 'state.json'
        initial = {'project': 'project', 'order': ['a', 'b']}
        picker.write_session_state(ui.state_path, initial)
        ui.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process = ui.process
        ui.back_to_print()
        self.assertEqual(ui.return_code, 3)
        self.assertIsNotNone(process.poll())
        self.assertEqual(picker.read_session_state(ui.state_path), initial)

    def test_drag_reorders_and_save_uses_the_shown_order(self):
        from types import SimpleNamespace
        ui = self.ui
        with fitz.open() as doc:
            for title in ('A', 'B', 'C'):
                doc.new_page().insert_text((20, 20), title)
            doc.save(ui.pdf)
        (ui.workdir / 'ready.json').write_text(json.dumps({'page_titles': ['A', 'B', 'C']}))
        ui.load_document()
        ui.root.update()
        first = ui.page_list.bbox(0)
        ui.start_drag(SimpleNamespace(y=first[1] + 10))
        ui.page_list.see(2)
        last = ui.page_list.bbox(2)
        drop = SimpleNamespace(x=15, y=last[1] + last[3] - 1)
        ui.drag_motion(drop)
        ui.root.update_idletasks()
        self.assertTrue(ui.drop_line.winfo_ismapped())
        self.assertEqual(ui.drag_position, 3)
        self.assertEqual(ui.drop_line.winfo_y(), min(ui.page_list.bbox(2)[1] + last[3], ui.page_list.winfo_height() - 3))
        ui.finish_drag(drop)
        self.assertFalse(ui.drop_line.winfo_ismapped())
        self.assertEqual(ui.page_order, [1, 2, 0])
        destination = self.root_path / 'reordered.pdf'
        with patch('tkinter.filedialog.asksaveasfilename', return_value=str(destination)):
            ui.save()
        with fitz.open(destination) as doc:
            self.assertEqual([page.get_text().strip() for page in doc], ['B', 'C', 'A'])

    def load_multiselect_pages(self):
        ui = self.ui
        with fitz.open() as doc:
            for title in ('A', 'A legend', 'B', 'C', 'D', 'E'):
                doc.new_page().insert_text((20, 20), title)
            doc.save(ui.pdf)
        (ui.workdir / 'ready.json').write_text(json.dumps({
            'page_titles': ['A', 'A legend', 'B', 'C', 'D', 'E'],
            'page_keys': ['a', 'a', 'b', 'c', 'd', 'e']}))
        ui.root.geometry('1000x760+0+0')
        ui.load_document()
        ui.root.update()
        return ui

    def click_preview_row(self, index, state=0):
        ui = self.ui
        ui.page_list.see(index)
        ui.root.update()
        box = ui.page_list.bbox(index)
        self.assertIsNotNone(box)
        ui.page_list.event_generate('<ButtonPress-1>', x=15, y=box[1] + 3, state=state)
        ui.page_list.event_generate('<ButtonRelease-1>', x=15, y=box[1] + 3, state=state)
        ui.root.update()

    def test_native_ctrl_shift_selection_and_group_drag_with_undo_redo(self):
        ui = self.load_multiselect_pages()
        self.click_preview_row(0)
        self.click_preview_row(3, state=4)  # Ctrl adds C.
        self.assertEqual(ui.selected_drawing_keys(), {'a', 'c'})
        self.assertEqual(ui.selection_status.get(), '2 drawings selected')
        ui.page_list.see(0)
        first = ui.page_list.bbox(0)
        ui.page_list.event_generate('<ButtonPress-1>', x=15, y=first[1] + 10)
        self.assertEqual(ui.selected_drawing_keys(), {'a', 'c'})
        ui.page_list.see(5)
        last = ui.page_list.bbox(5)
        ui.page_list.event_generate('<B1-Motion>', x=15, y=last[1] + last[3] - 1)
        ui.root.update_idletasks()
        self.assertTrue(ui.drop_line.winfo_ismapped())
        ui.page_list.event_generate('<ButtonRelease-1>', x=15, y=last[1] + last[3] - 1)
        ui.root.update()
        self.assertEqual(ui.page_order, [2, 4, 5, 0, 1, 3])
        self.assertEqual(ui.selected_drawing_keys(), {'a', 'c'})
        self.assertEqual(ui.page_list.curselection(), (3, 5))
        self.assertFalse(ui.drop_line.winfo_ismapped())
        ui.undo_order()
        self.assertEqual(ui.page_order, [0, 1, 2, 3, 4, 5])
        self.assertEqual(ui.page_list.curselection(), (0, 3))
        ui.redo_order()
        self.assertEqual(ui.page_order, [2, 4, 5, 0, 1, 3])
        ui.undo_order()
        self.click_preview_row(2)
        self.click_preview_row(4, state=1)  # Shift selects B through D.
        self.assertEqual(ui.selected_drawing_keys(), {'b', 'c', 'd'})
        ui.move_drawing('top')
        self.assertEqual(ui.page_order, [2, 3, 4, 0, 1, 5])
        self.assertEqual(ui.selected_drawing_keys(), {'b', 'c', 'd'})
        self.assertEqual(ui.redo_stack, [])  # A new edit starts a new history branch.
        self.assertIn('disabled', ui.redo_button.state())

    def test_group_buttons_and_select_all_preserve_relative_order(self):
        ui = self.load_multiselect_pages()
        ui.page_list.selection_clear(0, 'end')
        ui.page_list.selection_set(1)  # Legend selects drawing A for movement.
        ui.page_list.selection_set(3)
        ui.move_drawing('down')
        self.assertEqual(ui.page_order, [2, 0, 1, 4, 3, 5])
        ui.move_drawing('up')
        self.assertEqual(ui.page_order, [0, 1, 2, 3, 4, 5])
        ui.move_drawing('bottom')
        self.assertEqual(ui.page_order, [2, 4, 5, 0, 1, 3])
        self.assertEqual(ui.selected_drawing_keys(), {'a', 'c'})
        ui.select_all_pages()
        self.assertEqual(ui.selection_status.get(), '5 drawings selected')
        before = list(ui.page_order)
        history_size = len(ui.undo_stack)
        ui.move_drawing('up')
        self.assertEqual(ui.page_order, before)
        self.assertEqual(len(ui.undo_stack), history_size)
        ui.restore_order()
        self.assertEqual(ui.page_order, [0, 1, 2, 3, 4, 5])
        ui.undo_order()
        self.assertEqual(ui.page_order, before)
        # The selected order, not the order before Undo, is what gets saved.
        destination = self.root_path / 'multiple.pdf'
        with patch('tkinter.filedialog.asksaveasfilename', return_value=str(destination)):
            ui.save()
        with fitz.open(destination) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ['B', 'D', 'E', 'A', 'A legend', 'C'])

    def test_remove_groups_undo_redo_reorder_and_save_subset(self):
        ui = self.load_multiselect_pages()
        ui.show_page(1)  # Companion legend removes its drawing as well.
        ui.page_list.selection_set(3)
        ui.remove_button.invoke()
        self.assertEqual(ui.page_order, [2, 4, 5])
        self.assertEqual(ui.page_status.get(), 'Page 2 of 3')
        ui.navigate(10)
        ui.render_page()
        self.assertEqual(ui.page_number, 2)
        self.assertIn('disabled', ui.next_button.state())
        ui.undo_order()
        self.assertEqual(ui.page_order, [0, 1, 2, 3, 4, 5])
        self.assertEqual(ui.page_list.curselection(), (1, 3))
        self.assertEqual(ui.page_number, 1)
        ui.redo_order()
        self.assertEqual(ui.page_order, [2, 4, 5])
        ui.show_page(2)
        ui.move_drawing('top')
        self.assertEqual(ui.page_order, [5, 2, 4])
        ui.restore_order()
        self.assertEqual(ui.page_order, [2, 4, 5])  # Removed pages stay removed.
        ui.undo_order()
        destination = self.root_path / 'reduced.pdf'
        with patch('tkinter.filedialog.asksaveasfilename', return_value=str(destination)):
            ui.save()
        with fitz.open(destination) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ['E', 'B', 'D'])
        self.assertEqual(len(ui.document), 6)  # Original kept for Undo.

    def test_remove_all_delete_shortcut_empty_controls_and_undo(self):
        ui = self.load_multiselect_pages()
        ui.root.geometry('760x520+0+0')
        ui.root.update()
        self.assertGreater(ui.page_list.winfo_height(), 30)
        ui.select_all_pages()
        ui.page_list.focus_force()
        ui.root.update()
        ui.page_list.event_generate('<Delete>')
        ui.root.update()
        self.assertEqual(ui.page_order, [])
        self.assertEqual(ui.page_list.size(), 0)
        for button in (ui.save_button, ui.remove_button, ui.restore_button,
                       ui.previous_button, ui.next_button, ui.zoom_box):
            self.assertIn('disabled', button.state())
        self.assertNotIn('disabled', ui.undo_button.state())
        ui.navigate(1)
        ui.render_page()
        with patch('tkinter.filedialog.asksaveasfilename') as dialog:
            ui.save()
            dialog.assert_not_called()
        ui.restore_order()
        self.assertEqual(ui.page_order, [])
        ui.page_list.event_generate('<Control-z>')
        ui.root.update()
        self.assertEqual(ui.page_order, list(range(6)))
        self.assertEqual(ui.page_list.curselection(), tuple(range(6)))
        self.assertNotIn('disabled', ui.save_button.state())
        ui.page_list.event_generate('<Control-y>')
        ui.root.update()
        self.assertEqual(ui.page_order, [])

    def test_removed_pages_return_to_picker_including_empty_selection(self):
        ui = self.load_multiselect_pages()
        state = self.root_path / 'session.json'
        prefs = self.root_path / 'preferences.json'
        form = picker.PickerWindow(catalog(), self.root_path / 'choice.txt', prefs, state_path=state)
        form.root.withdraw()
        form.save_session()
        form.close()
        # Match this preview's synthetic keys to the catalog's real sheets.
        ui.details['page_keys'] = ['a', 'a', 'b', 'c', 'd', 'd']
        initial = picker.read_session_state(state)
        initial['order'] = ['a', 'b', 'c', 'd']
        picker.write_session_state(state, initial)
        ui.state_path = state
        ui.remove_selected_pages()
        self.assertEqual(ui.drawing_order(), ['b', 'c', 'd'])
        with patch.object(ui, 'close'):
            ui.back_to_print()
        self.assertEqual(picker.read_session_state(state)['order'], ['b', 'c', 'd'])
        resumed = picker.PickerWindow(catalog(), self.root_path / 'choice.txt', prefs, state_path=state)
        resumed.root.withdraw()
        try:
            self.assertEqual(resumed.model.selected_guids(), ['b', 'c', 'd'])
        finally:
            resumed.close()
        # Reuse the synthetic preview against its original launch state.
        picker.write_session_state(state, initial)
        ui.select_all_pages()
        ui.remove_selected_pages()
        ui.back_to_print()
        resumed = picker.PickerWindow(catalog(), self.root_path / 'choice.txt', prefs, state_path=state)
        resumed.root.withdraw()
        try:
            self.assertEqual(resumed.model.selected_guids(), [])
        finally:
            resumed.close()

    def test_thumbnails_load_visible_pages_and_follow_undo_and_reordering(self):
        ui = self.load_multiselect_pages()
        deadline = time.monotonic() + 3
        while 0 not in ui.page_list.images and time.monotonic() < deadline:
            ui.root.update()
            time.sleep(0.02)
        image = ui.page_list.images[0]
        self.assertLessEqual(image.width(), 200)
        self.assertLessEqual(image.height(), 108)
        self.assertEqual(ui.page_list.pages[0], (0, '1. A'))
        ui.show_page(0)
        ui.move_drawing('bottom')
        self.assertEqual(ui.page_list.pages[4], (0, '5. A'))
        self.assertIs(ui.page_list.images[0], image)
        ui.remove_selected_pages()
        self.assertNotIn(0, [original for original, _ in ui.page_list.pages])
        ui.undo_order()
        self.assertEqual(ui.page_list.pages[4], (0, '5. A'))
        self.assertIs(ui.page_list.images[0], image)
        ui.undo_order()
        self.assertEqual(ui.page_list.pages[0], (0, '1. A'))
        ui.page_list.focus_force()
        ui.root.update()
        ui.page_list.event_generate('<Down>', state=1)
        ui.root.update()
        self.assertEqual(ui.page_list.curselection(), (0, 1))
        ui.page_list.event_generate('<End>')
        ui.root.update()
        self.assertEqual(ui.page_list.curselection(), (5,))
        self.assertEqual(ui.page_number, 5)
        box = ui.page_list.bbox(5)
        self.assertGreaterEqual(box[1], 0)
        self.assertLessEqual(box[1] + box[3], ui.page_list.winfo_height())

    def test_thumbnail_cache_is_bounded_and_scroll_loads_later_pages(self):
        ui = self.ui
        with fitz.open() as doc:
            for number in range(15):
                page = doc.new_page(width=800 if number % 2 else 600, height=600 if number % 2 else 800)
                page.draw_rect(page.rect, color=None, fill=(number / 15, 0.3, 0.7))
                page.insert_text((20, 30), f'Sheet {number}')
            doc.save(ui.pdf)
        (ui.workdir / 'ready.json').write_text(json.dumps({'page_titles': [f'A-{i}' for i in range(15)]}))
        ui.load_document()
        ui.root.update()
        cards = ui.page_list
        cards.CACHE_LIMIT = 5
        for index in (0, 3, 6, 9, 12, 14):
            cards.see(index)
            deadline = time.monotonic() + 2
            while index not in cards.images and time.monotonic() < deadline:
                ui.root.update()
                time.sleep(0.02)
            self.assertIn(index, cards.images)
            self.assertLessEqual(len(cards.images), 5)
        self.assertNotIn(0, cards.images)
        self.assertGreater(cards.yview()[0], 0)
        # Portrait/landscape thumbnails preserve the drawing proportions.
        self.assertLess(cards.images[14].width(), cards.images[14].height())
        cards.see(0)
        deadline = time.monotonic() + 2
        while 0 not in cards.images and time.monotonic() < deadline:
            ui.root.update()
            time.sleep(0.02)
        self.assertIn(0, cards.images)  # Evicted images can be rendered again.
        self.assertEqual(cards.images[0].get(5, 5)[0], 0)
        ui.root.geometry('760x520+0+0')
        ui.root.update()
        self.assertGreaterEqual(cards.winfo_height(), cards.ROW_HEIGHT)

    def switch_page_view(self, mode):
        ui = self.ui
        ui.page_view.set(mode)
        ui.page_view_box.event_generate('<<ComboboxSelected>>')
        ui.root.update()
        self.assertEqual(ui.page_list.view_mode, mode)

    def test_view_switch_preserves_selection_current_page_history_and_order(self):
        ui = self.load_multiselect_pages()
        ui.show_page(2)
        ui.page_list.selection_set(4)
        ui.move_drawing('top')
        before = ui.order_snapshot()
        history = list(ui.undo_stack)
        self.switch_page_view('List')
        self.assertEqual(ui.order_snapshot(), before)
        self.assertEqual(ui.undo_stack, history)
        self.assertEqual(ui.page_list.ROW_HEIGHT, 26)
        self.assertIsNone(ui.page_list.thumbnail_job)
        self.assertTrue(ui.page_list_horizontal.winfo_ismapped())
        self.assertTrue(all(ui.page_list.type(item) != 'image' for item in ui.page_list.find_all()))
        self.switch_page_view('Thumbnails')
        self.assertEqual(ui.order_snapshot(), before)
        self.assertEqual(ui.undo_stack, history)
        self.assertEqual(ui.page_list.ROW_HEIGHT, 152)
        self.assertFalse(ui.page_list_horizontal.winfo_ismapped())
        ui.undo_order()
        self.assertEqual(ui.page_order, list(range(6)))

    def test_list_view_group_drag_remove_undo_and_save(self):
        ui = self.load_multiselect_pages()
        self.switch_page_view('List')
        self.click_preview_row(0)
        self.click_preview_row(3, state=4)
        self.assertEqual(ui.selected_drawing_keys(), {'a', 'c'})
        first, last = ui.page_list.bbox(0), ui.page_list.bbox(5)
        ui.page_list.event_generate('<ButtonPress-1>', x=15, y=first[1] + 5)
        ui.page_list.event_generate('<B1-Motion>', x=15, y=last[1] + last[3] - 1)
        ui.root.update_idletasks()
        self.assertTrue(ui.drop_line.winfo_ismapped())
        ui.page_list.event_generate('<ButtonRelease-1>', x=15, y=last[1] + last[3] - 1)
        ui.root.update()
        self.assertEqual(ui.page_order, [2, 4, 5, 0, 1, 3])
        ui.remove_selected_pages()
        self.assertEqual(ui.page_order, [2, 4, 5])
        self.switch_page_view('Thumbnails')
        ui.undo_order()
        self.assertEqual(ui.page_order, [2, 4, 5, 0, 1, 3])
        self.switch_page_view('List')
        ui.redo_order()
        destination = self.root_path / 'list-view.pdf'
        with patch('tkinter.filedialog.asksaveasfilename', return_value=str(destination)):
            ui.save()
        with fitz.open(destination) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ['B', 'D', 'E'])
        self.click_preview_row(0)
        self.click_preview_row(2, state=1)
        self.assertEqual(ui.page_list.curselection(), (0, 1, 2))

    def test_list_view_long_names_empty_selection_and_before_build(self):
        self.switch_page_view('List')  # Also works before the worker finishes.
        ui = self.load_multiselect_pages()
        ui.details['page_titles'][0] = 'Very long sheet title ' * 20
        ui.refresh_page_list()
        ui.show_page(0)
        ui.root.update()
        self.assertIsNone(ui.page_list.thumbnail_job)
        ui.page_list.xview_moveto(1)
        self.assertGreater(ui.page_list.xview()[0], 0)
        ui.select_all_pages()
        ui.remove_selected_pages()
        self.switch_page_view('Thumbnails')
        self.switch_page_view('List')
        self.assertEqual(ui.page_order, [])
        self.assertEqual(ui.page_list.size(), 0)
        ui.undo_order()
        self.assertEqual(ui.page_order, list(range(6)))
        self.switch_page_view('Thumbnails')
        self.assertLessEqual(ui.page_list.xview()[0], 0.01)

    def test_preview_view_persists_on_close_and_preserves_other_settings(self):
        original = picker.ExportPreferences(content='ALL', background='PDF',
                                            source_roots={'project': 'C:/Drawings'})
        picker.save_preferences(self.preferences_path, original)
        self.switch_page_view('List')
        self.assertEqual(picker.load_preferences(self.preferences_path),
                         preview.replace(original, preview_view='List'))
        self.ui.close()  # No export is required to remember a view choice.
        reopened = preview.PreviewWindow(self.manifest, start=False, preferences_path=self.preferences_path)
        reopened.root.withdraw()
        try:
            self.assertEqual(reopened.page_view.get(), 'List')
            self.assertEqual(reopened.page_list.view_mode, 'List')
            reopened.page_view.set('Thumbnails')
            reopened.change_page_view()
        finally:
            reopened.close()
        self.assertEqual(picker.load_preferences(self.preferences_path).preview_view, 'Thumbnails')

    def test_picker_save_preserves_preview_view_and_failed_view_save_is_nonfatal(self):
        form = picker.PickerWindow(catalog(), self.root_path / 'choice.txt', self.preferences_path)
        form.root.withdraw()
        try:
            # Preview changes the shared preference after the print form was opened.
            self.switch_page_view('List')
            form.save_current_preferences()
            self.assertEqual(picker.load_preferences(self.preferences_path).preview_view, 'List')
        finally:
            form.close()
        with patch.object(picker, 'save_preferences', side_effect=PermissionError('read only')):
            self.switch_page_view('Thumbnails')
        self.assertIn('could not be saved', self.ui.status.get())
        self.assertEqual(picker.load_preferences(self.preferences_path).preview_view, 'List')

    def test_real_worker_builds_finished_pages_and_enables_save(self):
        ui = self.ui
        ui.start_build()
        deadline = time.monotonic() + 20
        while ui.document is None and ui.process.poll() is None and time.monotonic() < deadline:
            ui.root.update()
            time.sleep(0.02)
        if ui.poll_job is not None:
            ui.root.after_cancel(ui.poll_job)
            ui.poll_job = None
        ui.poll_build()
        self.assertIsNotNone(ui.document)
        self.assertNotIn('disabled', ui.save_button.state())
        self.assertIn('500 LN FT', ui.document[0].get_text())

    def test_close_cancels_worker_and_never_saves_final_pdf(self):
        ui = self.ui
        ui.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process = ui.process
        ui.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(ui.workdir.exists())
        self.assertFalse((self.manifest.parent / 'export.pdf').exists())


if __name__ == '__main__':
    unittest.main()
