# SPDX-License-Identifier: AGPL-3.0-only
"""Finished-PDF preview and save window; launched by the PlanSwift bridge."""
from pathlib import Path
from dataclasses import replace
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

import fitz

MAX_PREVIEW_PIXELS = 12_000_000


def same_path(left, right):
    if os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right)):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def save_preview(pdf, destination, protected_sources=(), order=None):
    """Save reviewed order atomically, moving existing page objects with their links."""
    destination = Path(destination).absolute()
    if destination.suffix.lower() != '.pdf':
        raise ValueError('Choose a filename ending in .pdf.')
    if same_path(destination, pdf) or any(same_path(destination, source) for source in protected_sources):
        raise ValueError('That is an original source PDF or the temporary preview. Choose a different filename.')
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.takeoff-', suffix='.tmp', delete=False) as output:
            pending = Path(output.name)
        if order is None:
            shutil.copyfile(pdf, pending)
        else:
            with fitz.open(pdf) as doc:
                if (not order or any(type(i) is not int or not 0 <= i < len(doc) for i in order)
                        or len(set(order)) != len(order)):
                    raise ValueError('Choose at least one page, without duplicates or invalid page numbers.')
                if order == list(range(len(doc))):
                    shutil.copyfile(pdf, pending)
                else:
                    current = list(range(len(doc)))
                    for position, original in enumerate(order):
                        source = current.index(original)
                        if source != position:
                            doc.move_page(source, position)
                            current.insert(position, current.pop(source))
                    removed = len(order) < len(doc)
                    if removed:
                        doc.delete_pages(from_page=len(order), to_page=len(doc) - 1)
                    doc.save(pending, garbage=1 if removed else 0, deflate=True)
        with open(pending, 'rb+') as output:
            os.fsync(output.fileno())
        os.replace(pending, destination)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return destination


def render_preview(manifest, workdir):
    """Use an isolated copy of the manifest and never its requested output path."""
    import pdf_takeoff_builder as builder
    from pdf_source_paths import SourceResolver, clean_sheet_id
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    job = builder.parse_manifest(manifest)
    protected_sources = []
    if job.get('BackgroundMode', 'Raster').lower() == 'pdf':
        root = json.loads(job.get('SourceRootJSON', '""')) or job.get('ProjectRoot', '')
        if not root:
            raise ValueError('Choose the source project folder in the export window before previewing Source PDF.')
        resolver = SourceResolver(root, job.get('SourcePDFMode', 'Files').lower() == 'labels')
        for page in job['pages']:
            reference, error = resolver.match(page.get('TakeoffPath', ''), clean_sheet_id(page.get('PageName', '')))
            if reference:
                protected_sources.append(reference[0])
    isolated_manifest = workdir / 'preview.manifest.txt'
    shutil.copyfile(manifest, isolated_manifest)
    output = workdir / 'preview.pdf'
    details = builder.build(str(isolated_manifest), strict=True, output_path=str(output))
    project = job.get('ProjectName', '').strip()
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', project).strip(' .')[:100]
    details.update(protected_sources=protected_sources,
                   initial_folder=job.get('PreviewSaveFolder', '') or json.loads(job.get('SourceRootJSON', '""')),
                   filename=(filename + ' - Takeoff.pdf') if filename else 'Takeoff.pdf')
    with fitz.open(output) as doc:
        if len(details['page_titles']) != len(doc):
            raise ValueError('The preview page list does not match the rendered PDF.')
    (workdir / 'ready.json').write_text(json.dumps(details, ensure_ascii=True), encoding='utf-8')
    return details


def cleanup_bridge_manifest(manifest):
    """Only remove the bridge's exact private input, never a user's manifest."""
    manifest = Path(manifest).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if (manifest.name == 'preview.pdf.manifest.txt'
            and manifest.parent.name.startswith('PS_PDF_PREVIEW_')
            and manifest.parent.parent == temp):
        manifest.unlink(missing_ok=True)
        try:
            manifest.parent.rmdir()  # Only if empty; do not recurse.
        except OSError:
            pass


class PreviewWindow:
    def __init__(self, manifest, start=True, state_path=None, preferences_path=None):
        import tkinter as tk
        from tkinter import ttk
        from pdf_preview_thumbnails import PageThumbnailList
        from pdf_export_picker import default_preferences_path, load_preferences
        self.preferences_path = Path(preferences_path) if preferences_path is not None else default_preferences_path()
        preferences = load_preferences(self.preferences_path)
        self.tk, self.ttk = tk, ttk
        self.manifest = Path(manifest)
        self.state_path = Path(state_path) if state_path is not None else None
        self.return_code = 0
        self.page_order = []
        self.drag_start = None
        self.drag_position = None
        self.drag_keys = set()
        self.undo_stack, self.redo_stack = [], []
        self.temporary = tempfile.TemporaryDirectory(prefix='PS_PDF_VIEW_')
        self.workdir = Path(self.temporary.name)
        self.pdf = self.workdir / 'preview.pdf'
        self.document = self.process = self.log = self.photo = None
        self.poll_job = self.render_job = self.focus_job = None
        self.page_number = 0
        self.details = {}
        self.closed = False
        self.root = tk.Tk()
        self.root.title('Takeoff PDF - Preview')
        width = min(1300, self.root.winfo_screenwidth() - 80)
        height = min(900, self.root.winfo_screenheight() - 100)
        self.root.geometry(f'{width}x{height}')
        self.root.minsize(760, 520)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Escape>', lambda event: self.close())
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        style = ttk.Style(self.root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        toolbar = ttk.Frame(self.root, padding=10)
        toolbar.grid(row=0, column=0, sticky='ew')
        self.previous_button = ttk.Button(toolbar, text='Previous', command=lambda: self.navigate(-1))
        self.previous_button.pack(side='left')
        self.next_button = ttk.Button(toolbar, text='Next', command=lambda: self.navigate(1))
        self.next_button.pack(side='left', padx=(5, 15))
        self.page_status = tk.StringVar(value='Preparing preview...')
        ttk.Label(toolbar, textvariable=self.page_status).pack(side='left')
        self.progress = ttk.Progressbar(toolbar, mode='indeterminate', length=80)
        self.progress.pack(side='left', padx=10)
        self.progress.start(20)
        self.zoom = tk.StringVar(value='Fit page')
        self.zoom_box = ttk.Combobox(toolbar, textvariable=self.zoom, state='readonly', width=10,
                                    values=('Fit page', 'Fit width', '50%', '75%', '100%', '150%', '200%', '400%'))
        self.zoom_box.pack(side='right')
        self.zoom_box.bind('<<ComboboxSelected>>', self.schedule_render)
        ttk.Label(toolbar, text='Zoom').pack(side='right', padx=8)
        panes = ttk.Panedwindow(self.root, orient='horizontal')
        panes.grid(row=1, column=0, sticky='nsew', padx=10)
        pages = ttk.Frame(panes, width=210)
        pages.rowconfigure(1, weight=1)
        pages.columnconfigure(0, weight=1)
        page_header = ttk.Frame(pages)
        page_header.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))
        ttk.Label(page_header, text='PDF order').pack(side='left')
        self.page_view = tk.StringVar(value=preferences.preview_view)
        self.page_view_box = ttk.Combobox(page_header, textvariable=self.page_view,
                                         values=('Thumbnails', 'List'), state='readonly', width=12)
        self.page_view_box.pack(side='right')
        self.page_view_box.bind('<<ComboboxSelected>>', self.change_page_view)
        self.page_list = PageThumbnailList(pages, lambda: self.document)
        self.page_list.grid(row=1, column=0, sticky='nsew')
        self.drop_line = tk.Frame(self.page_list, background='#2878d0', height=3, borderwidth=0)
        bar = ttk.Scrollbar(pages, command=self.page_list.yview)
        bar.grid(row=1, column=1, sticky='ns')
        self.page_list.configure(yscrollcommand=bar.set)
        self.page_list_horizontal = ttk.Scrollbar(pages, orient='horizontal', command=self.page_list.xview)
        self.page_list_horizontal.grid(row=2, column=0, sticky='ew')
        self.page_list_horizontal.grid_remove()
        self.page_list.configure(xscrollcommand=self.page_list_horizontal.set)
        self.page_list.bind('<<ListboxSelect>>', self.select_page)
        self.page_list.bind('<ButtonPress-1>', self.start_drag)
        self.page_list.bind('<B1-Motion>', self.drag_motion)
        self.page_list.bind('<ButtonRelease-1>', self.finish_drag)
        self.page_list.bind('<Control-a>', self.select_all_pages)
        self.page_list.bind('<Delete>', self.remove_selected_pages)
        order_buttons = ttk.Frame(pages)
        order_buttons.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.order_buttons = []
        for column, (label, target) in enumerate((('Top', 'top'), ('Up', 'up'), ('Down', 'down'), ('Bottom', 'bottom'))):
            button = ttk.Button(order_buttons, text=label, width=5,
                                command=lambda target=target: self.move_drawing(target), state='disabled')
            button.grid(row=0, column=column, sticky='ew', padx=2, pady=2)
            self.order_buttons.append(button)
        for column in range(4):
            order_buttons.columnconfigure(column, weight=1)
        self.restore_button = ttk.Button(pages, text='Restore default order', command=self.restore_order, state='disabled')
        self.restore_button.grid(row=4, column=0, columnspan=2, sticky='ew', pady=4)
        history_buttons = ttk.Frame(pages)
        history_buttons.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(0, 4))
        history_buttons.columnconfigure(0, weight=1)
        history_buttons.columnconfigure(1, weight=1)
        self.undo_button = ttk.Button(history_buttons, text='Undo', command=self.undo_order, state='disabled')
        self.undo_button.grid(row=0, column=0, sticky='ew', padx=(0, 2))
        self.redo_button = ttk.Button(history_buttons, text='Redo', command=self.redo_order, state='disabled')
        self.redo_button.grid(row=0, column=1, sticky='ew', padx=(2, 0))
        self.remove_button = ttk.Button(pages, text='Remove selected pages',
                                        command=self.remove_selected_pages, state='disabled')
        self.remove_button.grid(row=6, column=0, columnspan=2, sticky='ew', pady=(0, 4))
        self.selection_status = tk.StringVar(value='0 drawings selected')
        ttk.Label(pages, textvariable=self.selection_status).grid(row=7, column=0, columnspan=2, sticky='w')
        ttk.Label(pages, text='Ctrl/Shift: select multiple pages.\nCtrl+A: select all. Delete: remove.\nCtrl+Z: undo. Ctrl+Y: redo.\nLegends stay with their drawings.',
                  foreground='#526276').grid(row=8, column=0, columnspan=2, sticky='w', pady=(3, 0))
        panes.add(pages, weight=0)
        area = ttk.Frame(panes)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, background='#62676e', highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        vbar = ttk.Scrollbar(area, command=self.canvas.yview)
        vbar.grid(row=0, column=1, sticky='ns')
        hbar = ttk.Scrollbar(area, orient='horizontal', command=self.canvas.xview)
        hbar.grid(row=1, column=0, sticky='ew')
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.canvas.bind('<Configure>', self.schedule_render)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<ButtonPress-1>', lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind('<B1-Motion>', lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        panes.add(area, weight=1)
        footer = ttk.Frame(self.root, padding=10)
        footer.grid(row=2, column=0, sticky='ew')
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value='Building all selected pages. No final PDF has been saved.')
        self.status_label = ttk.Label(footer, textvariable=self.status, wraplength=600)
        self.status_label.grid(row=0, column=0, sticky='w', padx=(0, 15))
        footer.bind('<Configure>', lambda e: self.status_label.configure(wraplength=max(180, e.width - 410)))
        self.save_button = ttk.Button(footer, text='Save PDF...', command=self.save, state='disabled')
        self.back_button = ttk.Button(footer, text='Back to print window', command=self.back_to_print)
        self.back_button.grid(row=0, column=1, padx=5)
        if self.state_path is None:
            self.back_button.state(['disabled'])
        self.save_button.grid(row=0, column=2, padx=5)
        self.close_button = ttk.Button(footer, text='Close', command=self.close)
        self.close_button.grid(row=0, column=3)
        ttk.Label(footer, text='Drag to pan. Ctrl + mouse wheel to zoom. Layer controls work in the saved PDF.',
                  foreground='#526276').grid(row=1, column=0, columnspan=4, sticky='w', pady=(7, 0))
        self.previous_button.state(['disabled'])
        self.next_button.state(['disabled'])
        self.zoom_box.state(['disabled'])
        self.root.bind('<Left>', lambda e: self.navigate(-1))
        self.root.bind('<Right>', lambda e: self.navigate(1))
        self.root.bind('<Control-z>', self.undo_order)
        self.root.bind('<Control-y>', self.redo_order)
        self.change_page_view(persist=False)
        if start:
            self.root.after_idle(self.start_build)

    def change_page_view(self, event=None, persist=True):
        self.clear_drop_cue()
        self.drag_start = None
        self.drag_keys = set()
        self.page_list.set_view(self.page_view.get())
        if self.page_view.get() == 'List':
            self.page_list_horizontal.grid()
        else:
            self.page_list_horizontal.grid_remove()
        self.page_list.focus_set()
        if persist:
            from pdf_export_picker import load_preferences, save_preferences
            try:
                current = load_preferences(self.preferences_path)
                save_preferences(self.preferences_path, replace(current, preview_view=self.page_view.get()))
            except OSError:
                self.status.set('View changed, but the preference could not be saved for next time.')

    def start_build(self):
        try:
            self.log = open(self.workdir / 'build.log', 'w', encoding='utf-8')
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--render', str(self.manifest), str(self.workdir)],
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.poll_job = self.root.after(100, self.poll_build)
        except Exception as exc:
            self.show_error(str(exc))

    def poll_build(self):
        self.poll_job = None
        if self.process.poll() is None:
            self.poll_job = self.root.after(100, self.poll_build)
            return
        self.log.close()
        self.log = None
        if self.process.returncode:
            error = self.workdir / 'error.txt'
            self.show_error(error.read_text(encoding='utf-8') if error.exists()
                            else 'The PDF builder could not finish. Close this window and try again.')
            return
        try:
            self.load_document()
        except Exception as exc:
            self.show_error(str(exc))

    def load_document(self):
        self.details = json.loads((self.workdir / 'ready.json').read_text(encoding='utf-8'))
        self.document = fitz.open(self.pdf)
        self.progress.stop()
        self.progress.pack_forget()
        self.page_order = list(range(len(self.document)))
        if 'page_keys' not in self.details:
            self.details['page_keys'] = [f'@page-{i}' for i in self.page_order]
        if len(self.details['page_keys']) != len(self.document):
            raise ValueError('The drawing order does not match the PDF.')
        for button in self.order_buttons + [self.restore_button]:
            button.state(['!disabled'])
        self.refresh_page_list()
        self.save_button.state(['!disabled'])
        self.zoom_box.state(['!disabled'])
        warnings = self.details.get('warnings', [])
        self.status.set('Preview ready. Use Save PDF to create your final export.'
                        + (f' {len(warnings)} drawing warnings; see Details.' if warnings else ''))
        if warnings:
            self.ttk.Button(self.status_label.master, text='Details',
                            command=lambda: self.show_details('\n\n'.join(warnings))).grid(row=2, column=0, sticky='w')
        self.show_page(0)

    def show_error(self, message):
        self.progress.stop()
        self.progress.pack_forget()
        self.save_button.state(['disabled'])
        self.page_status.set('Preview could not be built')
        self.status.set('No final PDF was created. Use Back to print window to adjust your choices.')
        self.show_details(message)

    def show_details(self, message):
        from tkinter.scrolledtext import ScrolledText
        window = self.tk.Toplevel(self.root)
        window.title('Preview details')
        window.geometry('720x430')
        window.transient(self.root)
        text = ScrolledText(window, wrap='word', padx=12, pady=12, font=('Segoe UI', 10))
        text.pack(fill='both', expand=True)
        text.insert('1.0', message)
        text.configure(state='disabled')
        self.ttk.Button(window, text='Close', command=window.destroy).pack(pady=8)
        window.bind('<Escape>', lambda e: (window.destroy(), 'break')[1])

    def refresh_page_list(self):
        self.page_list.set_pages([(original, f"{number}. {self.details['page_titles'][original]}")
                                  for number, original in enumerate(self.page_order, 1)])

    def drawing_order(self):
        return list(dict.fromkeys(self.details['page_keys'][i] for i in self.page_order))

    def selected_drawing_keys(self):
        return {self.details['page_keys'][self.page_order[i]] for i in self.page_list.curselection()}

    def update_order_controls(self):
        selected = self.selected_drawing_keys() if self.document is not None else set()
        count = len(selected)
        self.selection_status.set(f"{count} drawing{'s' if count != 1 else ''} selected")
        for button in self.order_buttons + [self.remove_button]:
            button.state(['!disabled'] if selected else ['disabled'])
        for button in (self.restore_button, self.save_button, self.zoom_box):
            button.state(['!disabled'] if self.page_order else ['disabled'])
        self.undo_button.state(['!disabled'] if self.undo_stack else ['disabled'])
        self.redo_button.state(['!disabled'] if self.redo_stack else ['disabled'])

    def order_snapshot(self):
        return {'order': list(self.page_order),
                'selected': [self.page_order[i] for i in self.page_list.curselection()],
                'current': self.page_order[self.page_number] if self.page_order else None}

    def restore_snapshot(self, snapshot):
        self.clear_drop_cue()
        self.drag_start = None
        self.drag_keys = set()
        previous_position = self.page_number
        self.page_order = list(snapshot['order'])
        self.refresh_page_list()
        for index, original in enumerate(self.page_order):
            if original in snapshot['selected']:
                self.page_list.selection_set(index)
        if snapshot['current'] in self.page_order:
            position = self.page_order.index(snapshot['current'])
        else:
            position = min(previous_position, max(0, len(self.page_order) - 1))
        self.show_page(position, select=not self.page_list.curselection())
        self.update_order_controls()

    def apply_drawing_order(self, groups):
        keys = self.details['page_keys']
        order = [i for key in groups for i in range(len(keys)) if keys[i] == key]
        if order == self.page_order:
            return
        before = self.order_snapshot()
        self.undo_stack.append(before)
        self.undo_stack = self.undo_stack[-50:]
        self.redo_stack.clear()
        self.restore_snapshot(dict(before, order=order))
        self.status.set('Print order changed. Save PDF uses this order; Back keeps it in the print window.')

    def undo_order(self, event=None):
        if self.undo_stack:
            self.redo_stack.append(self.order_snapshot())
            self.restore_snapshot(self.undo_stack.pop())
            self.status.set('Change undone. Save PDF uses the pages shown.')
        return 'break'

    def redo_order(self, event=None):
        if self.redo_stack:
            self.undo_stack.append(self.order_snapshot())
            self.restore_snapshot(self.redo_stack.pop())
            self.status.set('Change redone. Save PDF uses the pages shown.')
        return 'break'

    def remove_selected_pages(self, event=None):
        if self.document is None:
            return 'break'
        selected = self.selected_drawing_keys()
        if not selected:
            return 'break'
        count = len(self.page_order)
        self.apply_drawing_order([key for key in self.drawing_order() if key not in selected])
        removed = count - len(self.page_order)
        self.status.set(f"Removed {removed} PDF page{'s' if removed != 1 else ''}. "
                        'Undo (Ctrl+Z) restores them. Save PDF uses the remaining pages.')
        return 'break'

    def select_all_pages(self, event=None):
        if self.document is not None:
            self.page_list.selection_set(0, 'end')
            self.update_order_controls()
        return 'break'

    def move_drawing(self, target):
        if self.document is None:
            return
        groups = self.drawing_order()
        selected = self.selected_drawing_keys()
        if not selected:
            return
        if target in ('top', 'bottom'):
            moved = [key for key in groups if key in selected]
            remaining = [key for key in groups if key not in selected]
            groups = moved + remaining if target == 'top' else remaining + moved
        elif target == 'up':
            for i in range(1, len(groups)):
                if groups[i] in selected and groups[i - 1] not in selected:
                    groups[i - 1], groups[i] = groups[i], groups[i - 1]
        elif target == 'down':
            for i in range(len(groups) - 2, -1, -1):
                if groups[i] in selected and groups[i + 1] not in selected:
                    groups[i], groups[i + 1] = groups[i + 1], groups[i]
        self.apply_drawing_order(groups)

    def restore_order(self):
        if self.document is None:
            return
        from pdf_export_picker import read_session_state
        available = self.drawing_order()
        defaults = read_session_state(self.state_path).get('default_order', [])
        groups = [key for key in defaults if key in available]
        groups += [key for key in dict.fromkeys(self.details['page_keys']) if key in available and key not in groups]
        self.apply_drawing_order(groups)

    def clear_drop_cue(self):
        self.drop_line.place_forget()
        self.drag_position = None

    def start_drag(self, event):
        self.clear_drop_cue()
        self.drag_start = None
        self.drag_keys = set()
        if self.document is None:
            return
        index = self.page_list.nearest(event.y)
        box = self.page_list.bbox(index)
        if not box or not box[1] <= event.y <= box[1] + box[3]:
            return
        self.page_list.activate(index)
        if getattr(event, 'state', 0) & 5:
            self.page_list.select_click(index, event.state)
            return 'break'
        if index not in self.page_list.curselection():
            self.page_list.selection_clear(0, 'end')
            self.page_list.selection_set(index)
            self.page_list.selection_anchor(index)
        self.page_list.focus_set()
        self.drag_start = (index, event.y)
        self.drag_keys = self.selected_drawing_keys()
        self.show_page(index, select=False)
        return 'break'  # Keep the multi-selection intact while the mouse is down.

    def drop_target(self, event):
        # A drop is an insertion boundary, not a row replacement. Snap to the
        # outside of a drawing/legend group so the cue matches the actual move.
        width, height = self.page_list.winfo_width(), self.page_list.winfo_height()
        if not (0 <= getattr(event, 'x', 0) < width and 0 <= event.y < height):
            return None
        row = self.page_list.nearest(event.y)
        box = self.page_list.bbox(row)
        if not box:
            return None
        after = event.y >= box[1] + box[3] / 2
        keys = self.details['page_keys']
        key = keys[self.page_order[row]]
        group_rows = [i for i, original in enumerate(self.page_order) if keys[original] == key]
        edge = self.page_list.bbox(group_rows[-1] if after else group_rows[0])
        line_y = (edge[1] + edge[3] if after else edge[1]) if edge else (height - 3 if after else 1)
        position = self.drawing_order().index(key) + int(after)
        return position, max(1, min(line_y, height - 3))

    def drag_motion(self, event):
        if self.drag_start is None:
            return
        if abs(event.y - self.drag_start[1]) < 5:
            self.clear_drop_cue()
            return 'break'
        if event.y < 15:
            self.page_list.yview_scroll(-1, 'units')
        elif event.y > self.page_list.winfo_height() - 15:
            self.page_list.yview_scroll(1, 'units')
        target = self.drop_target(event)
        if target is None:
            self.clear_drop_cue()
        else:
            self.drag_position, line_y = target
            self.drop_line.place(x=2, y=line_y, relwidth=1, width=-4, bordermode='ignore')
            self.drop_line.lift()
        return 'break'

    def finish_drag(self, event):
        origin = self.drag_start
        target = self.drop_target(event) if origin is not None and abs(event.y - origin[1]) >= 5 else None
        selected = self.drag_keys
        self.drag_start = None
        self.drag_keys = set()
        self.clear_drop_cue()
        if origin is None:
            return
        if target is None:
            if abs(event.y - origin[1]) < 5:
                self.show_page(origin[0])  # A plain click selects just that page.
            return 'break'
        groups = self.drawing_order()
        moved = [key for key in groups if key in selected]
        remaining = [key for key in groups if key not in selected]
        destination = sum(key not in selected for key in groups[:target[0]])
        self.apply_drawing_order(remaining[:destination] + moved + remaining[destination:])
        return 'break'

    def back_to_print(self):
        from pdf_export_picker import read_session_state, write_session_state
        from tkinter import messagebox
        if self.state_path is None:
            return
        try:
            state = read_session_state(self.state_path)
            if not state or not isinstance(state.get('order'), list):
                raise ValueError('The print-window session is unavailable. Close and reopen the plugin.')
            if self.document is not None:
                order = self.drawing_order()
                if set(self.details['page_keys']) != set(state['order']):
                    raise ValueError('The preview no longer matches the print-window pages. Close and reopen the plugin.')
                state['order'] = order
                state['custom_order'] = True
                state['edited'] = True
            write_session_state(self.state_path, state)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Could not return to print window', str(exc), parent=self.root)
            return
        self.return_code = 3
        self.close()

    def select_page(self, event=None):
        selected = self.page_list.curselection()
        self.update_order_controls()
        if selected:
            active = self.page_list.index('active')
            self.show_page(active if active in selected else selected[0], select=False)

    def navigate(self, direction):
        if self.document is not None:
            self.show_page(min(len(self.page_order) - 1, max(0, self.page_number + direction)))

    def show_page(self, number, select=True):
        if self.document is None:
            return
        if not self.page_order:
            self.page_number = 0
            self.page_status.set('No pages selected')
            self.previous_button.state(['disabled'])
            self.next_button.state(['disabled'])
            self.update_order_controls()
            self.render_page()
            return
        number = min(max(0, number), len(self.page_order) - 1)
        self.page_number = number
        if select:
            self.page_list.selection_clear(0, 'end')
            self.page_list.selection_set(number)
            self.page_list.selection_anchor(number)
        self.page_list.activate(number)
        self.update_order_controls()
        self.page_list.see(number)
        self.page_status.set(f'Page {number + 1} of {len(self.page_order)}')
        self.previous_button.state(['disabled'] if number == 0 else ['!disabled'])
        self.next_button.state(['disabled'] if number + 1 == len(self.page_order) else ['!disabled'])
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.schedule_render()

    def schedule_render(self, event=None):
        if self.render_job is not None:
            self.root.after_cancel(self.render_job)
        self.render_job = self.root.after(100, self.render_page)

    def render_page(self):
        if self.render_job is not None:
            self.root.after_cancel(self.render_job)
        self.render_job = None
        if self.document is None:
            return
        if not self.page_order:
            self.photo = None
            self.canvas.delete('all')
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            self.canvas.create_text(24, 24, anchor='nw', fill='white',
                                    width=max(150, self.canvas.winfo_width() - 48),
                                    text='No pages remain.\nUndo to restore pages, or go Back to print window.')
            return
        try:
            page = self.document[self.page_order[self.page_number]]
            rect = page.rect
            width, height = max(100, self.canvas.winfo_width() - 32), max(100, self.canvas.winfo_height() - 32)
            choice = self.zoom.get()
            if choice == 'Fit page':
                scale = min(width / rect.width, height / rect.height)
            elif choice == 'Fit width':
                scale = width / rect.width
            else:
                scale = float(choice.rstrip('%')) / 100 * 96 / 72
            cap = math.sqrt(MAX_PREVIEW_PIXELS / (rect.width * rect.height))
            limited = scale > cap
            scale = min(scale, cap)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
            self.photo = self.tk.PhotoImage(master=self.root, data=pix.tobytes('ppm'), format='PPM')
            self.canvas.delete('all')
            x, y = max(16, (width + 32 - pix.width) / 2), max(16, (height + 32 - pix.height) / 2)
            self.canvas.create_image(x, y, image=self.photo, anchor='nw')
            self.canvas.configure(scrollregion=(0, 0, max(width + 32, pix.width + 32), max(height + 32, pix.height + 32)))
            self.page_status.set(f'Page {self.page_number + 1} of {len(self.page_order)}'
                                 + (' (preview zoom limit)' if limited else ''))
        except Exception as exc:
            self.show_error('This preview page could not be displayed: ' + str(exc))

    def wheel(self, event):
        if event.state & 4:  # Ctrl
            steps = ('Fit page', 'Fit width', '50%', '75%', '100%', '150%', '200%', '400%')
            index = steps.index(self.zoom.get())
            self.zoom.set(steps[min(len(steps) - 1, max(0, index + (1 if event.delta > 0 else -1)))])
            self.schedule_render()
        else:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def save(self):
        from tkinter import filedialog, messagebox
        if self.document is None or not self.page_order:
            return
        destination = filedialog.asksaveasfilename(parent=self.root, title='Save Takeoff PDF',
                      initialfile=self.details.get('filename', 'Takeoff.pdf'), defaultextension='.pdf',
                      initialdir=self.details.get('initial_folder') or None,
                      filetypes=[('PDF drawings', '*.pdf')])
        if not destination:
            return
        try:
            result = save_preview(self.pdf, destination, self.details.get('protected_sources', ()), self.page_order)
            self.status.set('Saved: ' + str(result))
            self.details['initial_folder'] = str(result.parent)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Could not save PDF', str(exc), parent=self.root)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.progress.stop()
        for job in (self.poll_job, self.render_job, self.focus_job):
            if job is not None:
                self.root.after_cancel(job)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.log is not None:
            self.log.close()
        if self.document is not None:
            self.document.close()
        self.root.destroy()
        self.temporary.cleanup()
        cleanup_bridge_manifest(self.manifest)

    def run(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.focus_job = self.root.after(300, lambda: self.root.attributes('-topmost', False))
        try:
            self.root.mainloop()
        finally:
            self.close()
        return self.return_code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--state', type=Path)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('workdir', nargs='?', type=Path)
    args = parser.parse_args(argv)
    if args.render:
        if args.workdir is None:
            parser.error('The render worker needs a private working folder.')
        try:
            render_preview(args.manifest, args.workdir)
            return 0
        except Exception as exc:
            (args.workdir / 'error.txt').write_text(str(exc), encoding='utf-8')
            traceback.print_exc()
            return 1
    return PreviewWindow(args.manifest, state_path=args.state).run()


if __name__ == '__main__':
    raise SystemExit(main())
