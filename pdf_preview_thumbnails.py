# SPDX-License-Identifier: AGPL-3.0-only
"""Scrollable, extended-selection page cards with lazily rendered PDF thumbnails."""
from collections import OrderedDict
import tkinter as tk
from tkinter import font as tkfont

import fitz


class PageThumbnailList(tk.Canvas):
    ROW_HEIGHT = 152
    IMAGE_WIDTH = 200
    IMAGE_HEIGHT = 108
    CACHE_LIMIT = 48

    def __init__(self, master, document_getter, **kwargs):
        super().__init__(master, width=232, background='#edf0f4',
                         highlightthickness=1, highlightbackground='#bcc5d0',
                         takefocus=True, yscrollincrement=1, **kwargs)
        self.document_getter = document_getter
        self.pages = []
        self.view_mode = 'Thumbnails'
        self.list_width = 1
        self.selected = set()
        self.active = self.anchor = 0
        self.images = OrderedDict()
        self.failed = set()
        self.thumbnail_job = None
        self.caption_font = tkfont.Font(self, family='Segoe UI', size=9)
        self.bind('<Configure>', lambda event: self.redraw())
        self.bind('<MouseWheel>', self._wheel)
        self.bind('<Up>', lambda event: self._key_select(-1, event))
        self.bind('<Down>', lambda event: self._key_select(1, event))
        self.bind('<Home>', lambda event: self._key_select(-len(self.pages), event))
        self.bind('<End>', lambda event: self._key_select(len(self.pages), event))
        self.bind('<space>', self._space)
        self.bind('<Destroy>', self._destroyed)

    def set_pages(self, pages):
        """Each entry is (original PDF page index, numbered sheet caption)."""
        self.pages = list(pages)
        self.selected.clear()
        self.active = self.anchor = 0
        self.list_width = max((self.caption_font.measure(title) + 20 for _, title in self.pages), default=1)
        self.redraw()

    def set_view(self, mode):
        if mode not in ('Thumbnails', 'List'):
            raise ValueError('Unknown preview list view.')
        if mode == self.view_mode:
            return
        if self.thumbnail_job is not None:
            self.after_cancel(self.thumbnail_job)
            self.thumbnail_job = None
        self.view_mode = mode
        self.ROW_HEIGHT = type(self).ROW_HEIGHT if mode == 'Thumbnails' else 26
        self.xview_moveto(0)
        self.redraw()
        self.see(self.active)

    def size(self):
        return len(self.pages)

    def index(self, index):
        if index == 'active':
            return self.active
        if index == 'end':
            return len(self.pages) - 1
        return int(index)

    def curselection(self):
        return tuple(sorted(self.selected))

    def selection_set(self, first, last=None):
        first = self.index(first)
        last = first if last is None else self.index(last)
        self.selected.update(range(max(0, first), min(last + 1, len(self.pages))))
        self.redraw()

    def selection_clear(self, first, last=None):
        first = self.index(first)
        last = first if last is None else self.index(last)
        self.selected.difference_update(range(first, last + 1))
        self.redraw()

    def selection_anchor(self, index):
        self.anchor = self.index(index)

    def activate(self, index):
        self.active = self.index(index)
        self.redraw()

    def nearest(self, y):
        return max(0, min(len(self.pages) - 1, int(self.canvasy(y) // self.ROW_HEIGHT)))

    def bbox(self, index):
        index = self.index(index)
        if not 0 <= index < len(self.pages):
            return None
        top = round(index * self.ROW_HEIGHT - self.canvasy(0))
        return (2, top, max(1, self.winfo_width() - 4), self.ROW_HEIGHT)

    def see(self, index):
        if not self.pages:
            return
        top = self.index(index) * self.ROW_HEIGHT
        viewport_top = self.canvasy(0)
        height = max(1, self.winfo_height() - 2)
        if top < viewport_top:
            self.yview_moveto(top / (len(self.pages) * self.ROW_HEIGHT))
        elif top + self.ROW_HEIGHT > viewport_top + height:
            # Align a card to its top, even when the pane is shorter than a card.
            target = top if height < self.ROW_HEIGHT else top + self.ROW_HEIGHT - height
            self.yview_moveto(target / (len(self.pages) * self.ROW_HEIGHT))

    def yview(self, *args):
        if len(args) == 3 and args[0] == 'scroll' and args[2] == 'units':
            args = (args[0], int(args[1]) * 32, args[2])
        result = super().yview(*args)
        if args:
            self.redraw()
        return result

    def yview_moveto(self, fraction):
        super().yview_moveto(fraction)
        self.redraw()

    def yview_scroll(self, number, what):
        super().yview_scroll(number * 32 if what == 'units' else number, what)
        self.redraw()

    def _wheel(self, event):
        self.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def select_click(self, index, state=0):
        self.focus_set()
        self.active = index
        if state & 1:  # Shift keeps the last plain/Ctrl click as the range anchor.
            interval = set(range(min(self.anchor, index), max(self.anchor, index) + 1))
            self.selected = self.selected | interval if state & 4 else interval
        elif state & 4:
            self.selected.symmetric_difference_update({index})
            self.anchor = index
        else:
            self.selected = {index}
            self.anchor = index
        self.redraw()
        self.event_generate('<<ListboxSelect>>')

    def _key_select(self, delta, event):
        if self.pages:
            index = min(len(self.pages) - 1, max(0, self.active + delta))
            if event.state & 4 and not event.state & 1:
                self.activate(index)
            else:
                self.select_click(index, event.state)
            self.see(index)
        return 'break'

    def _space(self, event):
        if self.pages:
            self.select_click(self.active, event.state)
        return 'break'

    def visible_indices(self):
        top = self.canvasy(0)
        first = max(0, int(top // self.ROW_HEIGHT))
        last = min(len(self.pages), int((top + self.winfo_height()) // self.ROW_HEIGHT) + 1)
        return range(first, last)

    def _caption(self, text, width):
        # Two lines beneath each thumbnail; preserve the beginning of sheet IDs.
        lines = []
        for _ in range(2):
            count = len(text)
            while count > 0 and self.caption_font.measure(text[:count]) > width:
                count -= 1
            if count == len(text):
                lines.append(text)
                break
            if len(lines) == 1:
                while count > 0 and self.caption_font.measure(text[:count] + '…') > width:
                    count -= 1
                lines.append(text[:count] + '…')
                break
            split = text.rfind(' ', 0, count + 1)
            if split > count // 2:
                count = split
            lines.append(text[:count])
            text = text[count:].lstrip()
        return '\n'.join(lines)

    def redraw(self):
        super().delete('all')
        width = max(120, self.winfo_width())
        content_width = max(width - 2, self.list_width) if self.view_mode == 'List' else width - 2
        self.configure(scrollregion=(0, 0, content_width, len(self.pages) * self.ROW_HEIGHT))
        for index in self.visible_indices():
            original, title = self.pages[index]
            top = index * self.ROW_HEIGHT
            selected = index in self.selected
            self.create_rectangle(3, top + 2, content_width - 2, top + self.ROW_HEIGHT - 2,
                                  fill='#dcecff' if selected else '#ffffff',
                                  outline='#2878d0' if selected else '#ced5df',
                                  width=2 if selected else 1)
            if self.view_mode == 'List':
                self.create_text(9, top + self.ROW_HEIGHT / 2, anchor='w',
                                 font=self.caption_font, text=title, fill='#133654')
                continue
            image = self.images.get(original)
            if image is not None:
                self.images.move_to_end(original)
                self.create_image(width / 2, top + 60, image=image)
            else:
                self.create_text(width / 2, top + 55,
                                 text='Preview unavailable' if original in self.failed else 'Loading preview…',
                                 fill='#526276', font=self.caption_font)
            self.create_text(width / 2, top + 116, anchor='n', font=self.caption_font,
                             text=self._caption(title, width - 18), fill='#133654', justify='center')
        if self.view_mode == 'Thumbnails' and self.thumbnail_job is None and any(self.pages[i][0] not in self.images
                                              and self.pages[i][0] not in self.failed
                                              for i in self.visible_indices()):
            self.thumbnail_job = self.after(20, self._render_next)

    def _render_next(self):
        self.thumbnail_job = None
        document = self.document_getter()
        if document is None or self.view_mode != 'Thumbnails':
            return
        for index in self.visible_indices():
            original = self.pages[index][0]
            if original in self.images or original in self.failed:
                continue
            try:
                page = document[original]
                scale = min(self.IMAGE_WIDTH / page.rect.width, self.IMAGE_HEIGHT / page.rect.height)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
                self.images[original] = tk.PhotoImage(master=self, data=pix.tobytes('ppm'), format='PPM')
                while len(self.images) > self.CACHE_LIMIT:
                    self.images.popitem(last=False)
            except Exception:
                # A thumbnail failure must not block saving or removing a page.
                self.failed.add(original)
            break  # Yield to selection/drag events between pages.
        self.redraw()

    def _destroyed(self, event):
        if event.widget is self:
            if self.thumbnail_job is not None:
                self.after_cancel(self.thumbnail_job)
                self.thumbnail_job = None
            self.images.clear()
