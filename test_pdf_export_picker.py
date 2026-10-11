# SPDX-License-Identifier: AGPL-3.0-only
"""Selection/protocol tests and real Tk widget tests (no PlanSwift required)."""
from pathlib import Path
from dataclasses import replace
import json
import tempfile
import unittest
from unittest.mock import patch

import pdf_export_picker as p


def catalog():
    return p.Catalog('session-123', 'Tést project', 'b', (
        p.Node('root', -1, 'Plans & Docs'),
        p.Node('rev1', 0, 'September'),
        p.Node('a', 1, 'A-101 | Floor plan', True, True, True),
        p.Node('b', 1, 'A-201 | Elevations', True, False, True),
        p.Node('c', 1, 'A-001 | Cover', True, False, False, True),
        p.Node('rev2', 0, 'October'),
        p.Node('d', 5, 'A-101 | Floor plan', True, True, True),
        p.Node('e', 5, 'A-301 | Details', True),
        p.Node('empty', 0, 'Empty folder'),
    ))


def write_request(path, data):
    lines = [p.REQUEST_HEADER, data.token, data.project, data.current]
    for n in data.nodes:
        lines.extend(['NODE', n.guid, str(n.parent), 'PAGE' if n.is_page else 'FOLDER',
                      n.name, str(int(n.visible)), str(int(n.takeoff)), str(int(n.marked)), n.takeoff_path])
    lines.append('END')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-16')


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.model = p.SelectionModel(catalog())

    def test_defaults_follow_mode_until_user_changes_selection(self):
        m = self.model
        self.assertEqual(m.selected_guids(), ['a', 'c', 'd'])
        m.set_mode('ALL')
        self.assertEqual(m.selected_guids(), ['a', 'b', 'c', 'd'])
        m.toggle(7)  # Blank sheet explicitly selected.
        m.set_mode('VISIBLE')
        self.assertEqual(m.selected_guids(), ['a', 'b', 'c', 'd', 'e'])
        m.select_defaults()
        self.assertEqual(m.selected_guids(), ['a', 'c', 'd'])

    def test_filtered_folder_changes_only_matches_and_keeps_hidden_choices(self):
        m = self.model
        m.filter('A-101')
        self.assertEqual(m.matches, {2, 6})
        self.assertEqual(m.shown, {0, 1, 2, 5, 6})
        self.assertEqual(m.state(0), 'on')
        m.toggle(0)
        self.assertEqual(m.selected_guids(), ['c'])
        self.assertEqual(m.state(0), 'off')
        m.toggle(5)
        self.assertEqual(m.selected_guids(), ['c', 'd'])
        self.assertEqual(m.state(0), 'mixed')
        m.filter('')
        self.assertEqual(m.selected_guids(), ['c', 'd'])

    def test_folder_search_includes_descendants_and_multiple_terms(self):
        m = self.model
        m.filter('OCTOBER')
        self.assertEqual(m.matches, {6, 7})
        m.choose(m.matches, True)
        m.filter('October floor')
        self.assertEqual(m.matches, {6})
        m.filter('no such sheet')
        self.assertEqual(m.matches, set())
        self.assertEqual(m.shown, set())
        m.choose(m.matches, False)
        self.assertEqual(m.selected_guids(), ['a', 'c', 'd', 'e'])

    def test_duplicate_names_preserve_identity_and_project_order(self):
        m = self.model
        m.choose(m.pages, False)
        m.toggle(6)
        m.toggle(2)
        self.assertEqual(m.selected_guids(), ['a', 'd'])
        m.toggle(2)
        self.assertEqual(m.selected_guids(), ['d'])

    def test_current_shortcut_replaces_selection_and_mode_keeps_it(self):
        m = self.model
        m.select_current()
        m.set_mode('DRAWING')
        self.assertEqual(m.selected_guids(), ['b'])
        self.assertEqual(m.state(1), 'mixed')
        self.assertEqual(m.state(8), 'off')
        m.toggle(8)
        self.assertEqual(m.selected_guids(), ['b'])

    def test_empty_project_and_missing_current_are_safe(self):
        m = p.SelectionModel(p.Catalog('session', 'Empty', '', ()))
        m.select_current()
        m.filter('a')
        self.assertEqual(m.selected_guids(), [])
        self.assertIsNone(m.current)


class ProtocolTests(unittest.TestCase):
    def test_source_root_is_ascii_safe_and_takeoff_paths_survive_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            request, result = Path(tmp) / 'request.txt', Path(tmp) / 'result.txt'
            original = catalog()
            data = replace(original, nodes=tuple(replace(n, takeoff_path=r'..\Pages\Plans & Docs\Révision')
                                                 if n.is_page else n for n in original.nodes))
            write_request(request, data)
            self.assertEqual(p.read_catalog(request), data)
            source = r'C:\Projects\Prøyecto 測試'
            p.write_result(result, p.SelectionModel(data), True, False, 'PDF', source_root=source)
            lines = result.read_bytes().decode('ascii').splitlines()
            self.assertEqual(json.loads(lines[7]), source)
            self.assertEqual(lines[8], 'PAGES')

    def test_old_script_gets_actionable_update_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.txt'
            path.write_text('PDF_EXPORT_PICKER_REQUEST_V1\nold\nproject\npage\nEND\n', encoding='utf-16')
            with self.assertRaisesRegex(ValueError, 'FULL REPLACEMENT'):
                p.read_catalog(path)

    def test_custom_page_order_and_label_mode_reach_result_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.txt'
            model = p.SelectionModel(catalog())
            model.sort_order('reverse')
            p.write_result(path, model, True, True, 'PDF_LABELS')
            lines = path.read_text().splitlines()
            self.assertEqual(lines[5:], ['PDF_LABELS', 'EXPORT', '""', 'PAGES', 'd', 'c', 'a', 'MARKS', 'END'])

    def test_utf16_catalog_preserves_unicode_hierarchy_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'request.txt'
            write_request(path, catalog())
            self.assertEqual(p.read_catalog(path), catalog())

    def test_invalid_catalogs_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'request.txt'
            write_request(path, catalog())
            good = path.read_text(encoding='utf-16')
            bad = [good.replace('REQUEST_V2', 'REQUEST_V9'), good[:-4],
                   good.replace('NODE\na\n1\nPAGE', 'NODE\na\n99\nPAGE'),
                   good.replace('NODE\nb\n1\nPAGE', 'NODE\nb\n2\nPAGE'),
                   good.replace('NODE\nd\n5\nPAGE', 'NODE\nA\n5\nPAGE')]
            for text in bad:
                with self.subTest(text=text):
                    path.write_text(text, encoding='utf-16')
                    with self.assertRaises(ValueError):
                        p.read_catalog(path)

    def test_result_is_atomic_ascii_in_project_order_and_drawing_disables_legend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.txt'
            model = p.SelectionModel(catalog())
            model.select_current()
            model.set_mode('DRAWING')
            p.write_result(path, model, True, False, 'PDF')
            self.assertEqual(path.read_bytes().decode('ascii').splitlines(),
                             [p.RESULT_HEADER, 'session-123', 'DRAWING', '0', '0', 'PDF',
                              'EXPORT', '""', 'PAGES', 'b', 'MARKS', 'END'])
            self.assertFalse(path.with_suffix('.txt.pending').exists())
            original = path.read_bytes()
            with patch.object(p.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    p.write_result(path, model, True, True, 'RASTER')
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(path.with_suffix('.txt.pending').exists())

    def test_no_result_for_empty_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.txt'
            model = p.SelectionModel(catalog())
            model.choose(model.pages, False)
            with self.assertRaises(ValueError):
                p.write_result(path, model, True, True, 'RASTER')
            self.assertFalse(path.exists())

    def test_cancel_exit_removes_stale_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            request, result = Path(tmp) / 'request.txt', Path(tmp) / 'result.txt'
            write_request(request, catalog())
            result.write_text('stale accepted result')
            with patch.object(p, 'PickerWindow') as window:
                window.return_value.run.return_value = 2
                self.assertEqual(p.main([str(request), str(result)]), 2)
            self.assertFalse(result.exists())


class PreferenceTests(unittest.TestCase):
    def test_settings_round_trip_and_failed_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings' / 'preferences.json'
            values = p.ExportPreferences('ALL', False, False, 'PDF', True, preview_view='List')
            p.save_preferences(path, values)
            self.assertEqual(p.load_preferences(path), values)
            with patch.object(p.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    p.save_preferences(path, p.ExportPreferences())
            self.assertEqual(p.load_preferences(path), values)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_missing_invalid_and_partially_invalid_settings_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'preferences.json'
            self.assertEqual(p.load_preferences(path), p.ExportPreferences())
            for content in ('broken JSON', '[]', 'null'):
                path.write_text(content)
                self.assertEqual(p.load_preferences(path), p.ExportPreferences())
            path.write_text('{"content":"bad","legend":"false","greyscale":false,'
                            '"background":"PDF","pdf_labels":1,"unknown":true}')
            self.assertEqual(p.load_preferences(path), p.ExportPreferences(greyscale=False, background='PDF'))


    def test_invalid_preview_view_uses_thumbnails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'preferences.json'
            for value in ('bad', '', None, 1):
                path.write_text(json.dumps({'preview_view': value}))
                self.assertEqual(p.load_preferences(path).preview_view, 'Thumbnails')


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.result = Path(self.temp.name) / 'result.txt'
        self.preferences_path = Path(self.temp.name) / 'preferences.json'
        self.ui = p.PickerWindow(catalog(), self.result, self.preferences_path)
        self.callback_errors = []
        self.ui.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.ui.root.withdraw()
        self.ui.root.update()

    def tearDown(self):
        try:
            self.ui.close()
        except self.ui.tk.TclError:
            pass
        self.temp.cleanup()
        self.assertEqual(self.callback_errors, [])

    def test_keyboard_toggle_changes_selection_and_counts(self):
        ui = self.ui
        ui.tree.focus('3')
        ui.space()
        self.assertEqual(ui.model.selected_guids(), ['a', 'b', 'c', 'd'])
        self.assertEqual(ui.status.get(), '4 of 5 pages selected')
        ui.select_matching(False)
        self.assertIn('disabled', ui.export_button.state())
        ui.select_matching(True)
        self.assertNotIn('disabled', ui.export_button.state())

    def test_search_restores_collapsed_folders_and_preserves_hidden_checks(self):
        ui = self.ui
        ui.tree.item('1', open=False)
        ui.search.set('A-101')
        ui.apply_search()
        self.assertTrue(ui.tree.item('1', 'open'))
        self.assertFalse(ui.tree.exists('3'))
        self.assertIn('disabled', ui.expand_button.state())
        ui.select_matching(False)
        self.assertEqual(ui.model.selected_guids(), ['c'])
        ui.search.set('')
        ui.apply_search()
        self.assertFalse(ui.tree.item('1', 'open'))
        self.assertTrue(ui.tree.exists('3'))
        self.assertNotIn('disabled', ui.expand_button.state())

    def test_pending_search_is_used_by_select_results(self):
        ui = self.ui
        ui.search.set('October')
        ui.select_matching(False)
        self.assertEqual(ui.model.selected_guids(), ['a', 'c'])
        self.assertEqual(ui.select_button.cget('text'), 'Select results')
        self.assertIn('2 selected outside this search', ui.selection_hint.get())

    def test_current_shortcut_clears_search_and_reveals_page(self):
        ui = self.ui
        ui.search.set('October')
        ui.apply_search()
        ui.select_current()
        self.assertEqual(ui.search.get(), '')
        self.assertEqual(ui.tree.focus(), '3')
        self.assertEqual(ui.model.selected_guids(), ['b'])

    def test_drawing_mode_preserves_legend_preference_for_return_to_takeoff(self):
        ui = self.ui
        ui.mode.set('DRAWING')
        ui.change_mode()
        self.assertIn('disabled', ui.legend_check.state())
        self.assertTrue(ui.legend.get())
        ui.mode.set('VISIBLE')
        ui.change_mode()
        self.assertNotIn('disabled', ui.legend_check.state())
        ui.accept()
        self.assertTrue(ui.accepted)
        self.assertEqual(self.result.read_text().splitlines()[2:6], ['VISIBLE', '1', '1', 'RASTER'])

    def test_window_close_cancels_without_writing_result(self):
        ui = self.ui
        ui.cancel()
        self.assertFalse(ui.accepted)
        self.assertFalse(self.result.exists())
        self.assertTrue(self.preferences_path.exists())

    def test_cancel_remembers_options_and_restores_disabled_controls_correctly(self):
        ui = self.ui
        ui.mode.set('DRAWING')
        ui.change_mode()
        ui.greyscale.set(False)
        ui.background.set('PDF')
        ui.change_background()
        ui.pdf_labels.set(True)
        ui.cancel()
        self.assertEqual(p.load_preferences(self.preferences_path),
                         p.ExportPreferences('DRAWING', True, False, 'PDF', True))
        self.ui = restored = p.PickerWindow(catalog(), self.result, self.preferences_path)
        restored.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        restored.root.withdraw()
        restored.root.update()
        self.assertEqual(restored.mode.get(), 'DRAWING')
        self.assertEqual(restored.model.selected_guids(), ['a', 'b', 'c', 'd'])
        self.assertIn('disabled', restored.legend_check.state())
        self.assertTrue(restored.legend.get())
        self.assertFalse(restored.greyscale.get())
        self.assertEqual(restored.background.get(), 'PDF')
        self.assertTrue(restored.pdf_labels.get())
        self.assertNotIn('disabled', restored.pdf_labels_check.state())

    def test_accept_remembers_all_settings_even_when_pdf_label_option_is_disabled(self):
        ui = self.ui
        ui.mode.set('ALL')
        ui.change_mode()
        ui.legend.set(False)
        ui.greyscale.set(False)
        ui.pdf_labels.set(True)
        ui.accept()
        self.assertEqual(p.load_preferences(self.preferences_path),
                         p.ExportPreferences('ALL', False, False, 'RASTER', True))
        self.assertEqual(self.result.read_text().splitlines()[2:7], ['ALL', '0', '0', 'RASTER', 'EXPORT'])

    def test_pdf_label_checkbox_is_optional_and_only_affects_pdf(self):
        ui = self.ui
        self.assertIn('disabled', ui.pdf_labels_check.state())
        ui.background.set('PDF')
        ui.change_background()
        self.assertNotIn('disabled', ui.pdf_labels_check.state())
        ui.pdf_labels.set(True)
        ui.accept()
        self.assertEqual(self.result.read_text().splitlines()[5], 'PDF_LABELS')

    def test_source_help_and_marking_help_are_accessible(self):
        ui = self.ui
        self.assertTrue(ui.source_help_button.cget('image'))
        self.assertEqual(str(ui.source_help_button.cget('compound')), 'image')
        self.assertTrue(ui.mark_help_button.cget('image'))
        self.assertEqual(str(ui.mark_help_button.cget('compound')), 'left')
        ui.source_help_button.invoke()
        ui.help_window.withdraw()
        self.assertTrue(ui.help_window.winfo_exists())
        self.assertTrue(ui.source_help_text.tag_ranges('bold'))
        self.assertNotIn('**', ui.source_help_text.get('1.0', 'end'))
        self.assertIn('QUICK START', ui.source_help_text.get('1.0', '3.0'))
        self.assertIn('ONE PDF PER SHEET', p.SOURCE_PDF_HELP)
        self.assertIn('page label', p.SOURCE_PDF_HELP)
        with patch('tkinter.messagebox.showinfo') as show:
            ui.mark_help_button.invoke()
        self.assertIn('Hold Ctrl', show.call_args.args[1])
        self.assertIn('unflag', show.call_args.args[1])

    def test_scroll_exposes_complete_shortcut_hint_at_minimum_window_height(self):
        ui = self.ui
        ui.root.attributes('-alpha', 0.0)
        ui.root.deiconify()
        ui.root.geometry('940x650')
        ui.root.update()
        ui.settings_canvas.yview_moveto(1)
        ui.root.update()
        hint_bottom = ui.shortcut_hint.winfo_rooty() + ui.shortcut_hint.winfo_height()
        canvas_bottom = ui.settings_canvas.winfo_rooty() + ui.settings_canvas.winfo_height()
        self.assertLessEqual(hint_bottom, canvas_bottom)
        self.assertGreaterEqual(ui.shortcut_hint.winfo_height(), ui.shortcut_hint.winfo_reqheight())
        self.assertGreaterEqual(ui.shortcut_hint.winfo_width(), ui.shortcut_hint.winfo_reqwidth())
        self.assertGreaterEqual(ui.restore_order_button.winfo_width(), ui.restore_order_button.winfo_reqwidth())
        self.assertGreaterEqual(ui.order_tree.winfo_height(), 75)
        self.assertGreaterEqual(ui.tree.winfo_height(), 50)

    def test_selected_panel_reorders_and_removes_pages_without_losing_hidden_selection(self):
        ui = self.ui
        self.assertEqual(ui.order_tree.get_children(), ('2', '4', '6'))
        ui.order_tree.selection_set('6')
        ui.move_selected_to(0)
        self.assertEqual(ui.model.selected_guids(), ['d', 'a', 'c'])
        ui.search.set('October')
        ui.apply_search()
        self.assertEqual(ui.order_tree.get_children(), ('6', '2', '4'))
        ui.order_tree.selection_set('2')
        ui.remove_selected_order_rows()
        self.assertEqual(ui.model.selected_guids(), ['d', 'c'])
        self.assertEqual(ui.model.checked, {4, 6})

    def test_drag_gesture_reorders_rows(self):
        ui = self.ui
        ui.root.attributes('-alpha', 0.0)
        ui.root.deiconify()
        ui.root.update()
        ui.order_tree.see('6')
        ui.root.update()
        x, y, width, height = ui.order_tree.bbox('6')
        _, dest_y, _, dest_height = ui.order_tree.bbox('2')
        ui.order_tree.event_generate('<ButtonPress-1>', x=x + 60, y=y + height // 2)
        ui.order_tree.event_generate('<B1-Motion>', x=x + 60, y=dest_y + 2)
        ui.order_tree.event_generate('<ButtonRelease-1>', x=x + 60, y=dest_y + 2)
        ui.root.update()
        self.assertEqual(ui.model.selected_guids(), ['d', 'a', 'c'])
        self.assertEqual(ui.order_tree.item('6', 'values')[0], '1')

    def test_restore_default_button_keeps_selection_flags_and_search(self):
        ui = self.ui
        ui.change_marks({2}, True)
        ui.sort_selected('reverse')
        ui.search.set('October')
        ui.apply_search()
        checked = set(ui.model.checked)
        flags = set(ui.model.marks)
        ui.restore_order_button.invoke()
        self.assertEqual(ui.order_tree.get_children(), ('2', '4', '6'))
        self.assertFalse(ui.model.custom_order)
        self.assertEqual(ui.model.checked, checked)
        self.assertEqual(ui.model.marks, flags)
        self.assertEqual(ui.search.get(), 'October')
        self.assertEqual(ui.order_tree.item('2', 'image'), (str(ui.print_flag_icon),))

    def test_print_cell_click_toggles_only_flag_without_selecting_or_dragging(self):
        ui = self.ui
        ui.root.attributes('-alpha', 0.0)
        ui.root.deiconify()
        ui.sort_selected('reverse')
        ui.search.set('October')
        ui.apply_search()
        ui.order_tree.selection_set(('4', '6'))
        ui.root.update()
        before_order = list(ui.model.order)
        before_checked = set(ui.model.checked)
        x, y, width, height = ui.order_tree.bbox('2', '#0')
        ui.order_tree.event_generate('<ButtonPress-1>', x=x + 20, y=y + height // 2, time=1000)
        ui.order_tree.event_generate('<B1-Motion>', x=x + 20, y=y - 30, time=1100)
        ui.order_tree.event_generate('<ButtonRelease-1>', x=x + 20, y=y - 30, time=1200)
        ui.root.update()
        self.assertIn(2, ui.model.marks)
        self.assertEqual(ui.order_tree.item('2', 'image'), (str(ui.print_flag_icon),))
        self.assertEqual(ui.model.order, before_order)
        self.assertEqual(ui.model.checked, before_checked)
        self.assertEqual(set(ui.order_tree.selection()), {'4', '6'})
        ui.order_tree.event_generate('<ButtonPress-1>', x=x + 20, y=y + height // 2, time=2000)
        ui.order_tree.event_generate('<ButtonRelease-1>', x=x + 20, y=y + height // 2, time=2100)
        ui.root.update()
        self.assertNotIn(2, ui.model.marks)
        self.assertEqual(ui.model.mark_changes(), [])
        self.assertEqual(ui.model.order, before_order)

    def test_text_width_limits_search_and_full_folder_hover(self):
        self.ui.close()
        long_folder = 'Long permit drawing set / ' * 30
        long_sheet = 'A-101 | ' + 'Enlarged amenity floor plan ' * 30
        self.ui = ui = p.PickerWindow(p.Catalog('widths', 'Widths', '', (
            p.Node('root', -1, 'Plans & Docs'),
            p.Node('folder', 0, long_folder),
            p.Node('long', 1, long_sheet, True, True, True),
            p.Node('brief', 0, 'Brief'),
            p.Node('short', 3, 'Short', True, True, True),
        )), self.result, self.preferences_path)
        ui.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        ui.root.attributes('-alpha', 0.0)
        ui.root.geometry('1400x900')
        ui.root.update()
        self.assertEqual(ui.pages_panel_width, p.PAGES_PANE_WIDTH[1])
        self.assertLessEqual(ui.pages_panel.winfo_width(), p.PAGES_PANE_WIDTH[1])
        self.assertGreaterEqual(ui.order_panel.winfo_width(), 395)
        for name, (minimum, maximum) in p.ORDER_COLUMN_WIDTHS.items():
            self.assertEqual(ui.order_tree.column(name, 'width'), maximum)

        ui.order_tree.xview_moveto(1)
        ui.root.update()
        x, y, width, height = ui.order_tree.bbox('2', 'folder')
        ui.order_tree.event_generate('<Motion>', x=max(5, x + 15), y=y + height // 2)
        ui.root.update()
        self.assertIsNotNone(ui.cell_tip_job)
        ui.root.after_cancel(ui.cell_tip_job)
        ui.show_cell_tip()
        ui.cell_tip.withdraw()
        self.assertEqual(ui.cell_tip_label.cget('text'), ui.model.paths[1])
        ui.order_tree.event_generate('<Leave>')
        self.assertIsNone(ui.cell_tip)
        self.assertIsNone(ui.cell_tip_job)

        ui.search.set('Short')
        ui.apply_search()
        ui.root.update()
        self.assertEqual(ui.pages_panel_width, p.PAGES_PANE_WIDTH[0])
        self.assertLess(ui.pages_panel.winfo_width(), p.PAGES_PANE_WIDTH[1])
        self.assertEqual(ui.order_tree.column('sheet', 'width'), p.ORDER_COLUMN_WIDTHS['sheet'][1])
        ui.model.choose({2}, False)
        ui.refresh_selection()
        ui.root.update()
        for name, (minimum, maximum) in p.ORDER_COLUMN_WIDTHS.items():
            self.assertEqual(ui.order_tree.column(name, 'width'), minimum)
        ui.root.geometry('940x650')
        ui.root.update()
        self.assertGreaterEqual(ui.order_tree.winfo_height(), 75)

    def test_bulk_marks_show_on_rows_and_save_without_export_selection(self):
        ui = self.ui
        ui.change_marks({2, 6}, True)
        self.assertEqual(ui.tree.item('2', 'text').strip(), ui.model.nodes[2].name)
        self.assertEqual(ui.tree.item('2', 'image'), (str(ui.icons['print', 'on']),))
        self.assertEqual(ui.order_tree.item('2', 'image'), (str(ui.print_flag_icon),))
        self.assertEqual(ui.model.mark_changes(), [('a', True), ('d', True)])
        ui.select_matching(False)
        self.assertIn('disabled', ui.export_button.state())
        ui.save_marks_only()
        lines = self.result.read_text().splitlines()
        self.assertEqual(lines[6:], ['MARKS_ONLY', '""', 'PAGES', 'MARKS', 'a', '1', 'd', '1', 'END'])

    def test_clear_marks_undo_and_select_marked(self):
        ui = self.ui
        ui.change_marks(ui.model.pages, False)
        self.assertEqual(ui.model.mark_changes(), [('c', False)])
        self.assertEqual(ui.tree.item('4', 'image'), (str(ui.icons['page', 'on']),))
        self.assertFalse(ui.order_tree.item('4', 'image'))
        ui.undo_mark_changes()
        self.assertEqual(ui.tree.item('4', 'image'), (str(ui.icons['print', 'on']),))
        self.assertEqual(ui.order_tree.item('4', 'image'), (str(ui.print_flag_icon),))
        self.assertEqual(ui.model.mark_changes(), [])
        ui.search.set('October')
        ui.apply_search()
        ui.select_marked_pages()
        self.assertEqual(ui.model.selected_guids(), ['c'])
        self.assertEqual(ui.search.get(), '')

    def test_cancel_saves_flag_and_unflag_edits_without_exporting(self):
        ui = self.ui
        ui.change_marks({2}, True)
        ui.change_marks({4}, False)
        ui.select_matching(False)
        ui.cancel()
        self.assertTrue(ui.accepted)
        self.assertEqual(self.result.read_text().splitlines()[6:],
                         ['MARKS_ONLY', '""', 'PAGES', 'MARKS', 'a', '1', 'c', '0', 'END'])

    def test_titlebar_close_saves_flags_through_same_path_as_cancel(self):
        ui = self.ui
        ui.change_marks({2}, True)
        ui.root.tk.call(ui.root.protocol('WM_DELETE_WINDOW'))
        self.assertEqual(self.result.read_text().splitlines()[6], 'MARKS_ONLY')

    def test_cancel_save_failure_keeps_flags_and_window_for_retry(self):
        ui = self.ui
        ui.change_marks({2}, True)
        with patch.object(p, 'write_result', side_effect=OSError('disk error')), \
                patch('tkinter.messagebox.showerror') as error:
            ui.cancel()
        error.assert_called_once()
        self.assertTrue(ui.root.winfo_exists())
        self.assertEqual(ui.model.mark_changes(), [('a', True)])
        self.assertFalse(ui.accepted)
        self.assertFalse(self.result.exists())
        ui.cancel()
        self.assertEqual(self.result.read_text().splitlines()[6], 'MARKS_ONLY')

    def test_source_check_includes_selected_pages_hidden_by_search(self):
        ui = self.ui
        ui.background.set('PDF')
        ui.change_background()
        ui.source_root.set(self.temp.name)
        ui.search.set('October')
        ui.apply_search()
        with patch('pdf_source_paths.check_source_pages', return_value=([], [('A-101', 'Missing')])) as check, \
                patch.object(ui, 'show_source_check_results'):
            self.assertFalse(ui.check_selected_sources())
        self.assertEqual([x['PageName'] for x in check.call_args.args[1]],
                         [ui.model.nodes[i].name for i in (2, 4, 6)])
        ui.select_matching(False)
        self.assertEqual(ui.source_check_status.get(), '')

    def test_failed_source_check_blocks_export_but_cancel_still_saves_flags(self):
        ui = self.ui
        ui.background.set('PDF')
        ui.change_background()
        ui.source_root.set(str(Path(self.temp.name) / 'missing'))
        ui.change_marks({2}, True)
        with patch.object(ui, 'show_source_check_results') as report:
            ui.accept()
        report.assert_called_once()
        self.assertFalse(ui.accepted)
        self.assertFalse(self.result.exists())
        ui.cancel()
        self.assertEqual(self.result.read_text().splitlines()[6:8], ['MARKS_ONLY', '""'])

    def test_preview_requires_source_folder_and_preserves_choices_if_browse_is_canceled(self):
        ui = self.ui
        ui.background.set('PDF')
        ui.change_background()
        ui.source_root.set('')
        with patch.object(ui, 'browse_source_root', return_value=False):
            ui.accept('PREVIEW')
        self.assertFalse(ui.accepted)
        self.assertFalse(self.result.exists())
        ui.source_root.set(self.temp.name)
        with patch.object(ui, 'check_selected_sources', return_value=True) as check:
            ui.accept('PREVIEW')
        check.assert_called_once_with(show_success=False)
        lines = self.result.read_text(encoding='ascii').splitlines()
        self.assertEqual(lines[6], 'PREVIEW')
        self.assertEqual(json.loads(lines[7]), self.temp.name)

    def test_preview_is_disabled_without_selected_pages(self):
        self.ui.select_matching(False)
        self.assertIn('disabled', self.ui.preview_button.state())
        self.ui.accept('PREVIEW')
        self.assertFalse(self.result.exists())

    def test_source_folder_preferences_belong_to_the_project(self):
        ui = self.ui
        ui.source_root.set(self.temp.name)
        ui.cancel()
        settings = p.load_preferences(self.preferences_path)
        self.assertEqual(settings.source_roots, {'root': self.temp.name})
        self.ui = restored = p.PickerWindow(catalog(), self.result, self.preferences_path)
        restored.root.withdraw()
        self.assertEqual(restored.source_root.get(), self.temp.name)
        restored.close()
        other = replace(catalog(), nodes=(replace(catalog().nodes[0], guid='other-root'), *catalog().nodes[1:]))
        self.ui = different = p.PickerWindow(other, self.result, self.preferences_path)
        different.root.withdraw()
        self.assertEqual(different.source_root.get(), '')


class PageOrderTests(unittest.TestCase):
    def test_custom_order_survives_selection_changes_search_and_modes(self):
        m = p.SelectionModel(catalog())
        m.move_order({6}, 0)
        m.filter('September')
        m.choose({3}, True)
        m.set_mode('ALL')
        self.assertEqual(m.selected_guids(), ['d', 'a', 'c', 'b'])
        m.choose({2}, False)
        self.assertEqual(m.selected_guids(), ['d', 'c', 'b'])
        m.sort_order('project')
        m.choose({2}, True)
        self.assertEqual(m.selected_guids(), ['a', 'b', 'c', 'd'])

    def test_multirow_moves_keep_relative_order_and_boundaries(self):
        m = p.SelectionModel(catalog())
        m.choose(m.pages, True)
        m.move_order({3, 6}, 0)
        self.assertEqual(m.order, [3, 6, 2, 4, 7])
        m.shift_order({3, 6}, -1)
        self.assertEqual(m.order, [3, 6, 2, 4, 7])
        m.shift_order({3, 6}, 1)
        self.assertEqual(m.order, [2, 3, 6, 4, 7])
        m.move_order({3, 6}, len(m.order))
        self.assertEqual(m.order, [2, 4, 7, 3, 6])
        m.shift_order({3, 6}, 1)
        self.assertEqual(m.order, [2, 4, 7, 3, 6])

    def test_sort_ids_uses_natural_numbers_and_keeps_duplicate_sheets(self):
        data = p.Catalog('session', 'Sort', '', tuple(
            p.Node(str(i), -1, name, True, True, True)
            for i, name in enumerate(('A-10 | Plan', 'A-2 | Plan', '*A-1 | Copy', 'A-1 | Original'))))
        m = p.SelectionModel(data)
        m.sort_order('sheet')
        self.assertEqual(m.order, [2, 3, 1, 0])
        m.sort_order('sheet_desc')
        self.assertEqual(m.order[:2], [0, 1])
        self.assertEqual(set(m.order), {0, 1, 2, 3})
        m.sort_order('project')
        self.assertEqual(m.order, [0, 1, 2, 3])

    def test_shortcuts_restore_default_order(self):
        m = p.SelectionModel(catalog())
        m.sort_order('reverse')
        m.select_defaults()
        self.assertEqual(m.selected_guids(), ['a', 'c', 'd'])
        self.assertFalse(m.custom_order)


class PrintMarkTests(unittest.TestCase):
    def test_marks_change_only_requested_pages_and_default_selection_uses_them(self):
        m = p.SelectionModel(catalog())
        m.filter('October')
        m.set_marks({7}, True)
        m.set_marks({4}, False)
        self.assertEqual(m.mark_changes(), [('c', False), ('e', True)])
        m.select_defaults()
        self.assertEqual(m.selected_guids(), ['a', 'd', 'e'])
        self.assertEqual(m.nodes[4].marked, True)  # original catalog is immutable

    def test_clearing_marks_then_restoring_them_emits_only_actual_changes(self):
        m = p.SelectionModel(catalog())
        m.set_marks(m.pages, False)
        m.set_marks({4}, True)
        self.assertEqual(m.mark_changes(), [])
        with self.assertRaises(ValueError):
            m.set_marks({0}, True)  # folders are never written as pages


if __name__ == '__main__':
    unittest.main()
