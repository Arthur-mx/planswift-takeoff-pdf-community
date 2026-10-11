# SPDX-License-Identifier: AGPL-3.0-only
"""External PlanSwift page picker. Run --demo to preview.

PlanSwift writes a UTF-16 catalog using FileSystemObject. The response is ASCII
and atomic so its small TStringList reader never sees a partially saved choice.
Source checks use the renderer's existing PyMuPDF dependency. No PlanSwift
COM calls or changes to project data are made by this window.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import os
import re
import sys
import tempfile
import traceback


REQUEST_HEADER = "PDF_EXPORT_PICKER_REQUEST_V2"
RESULT_HEADER = "PDF_EXPORT_PICKER_RESULT_V5"
MODES = ("VISIBLE", "ALL", "DRAWING")
PAGE_ITEM_MODES = ("VISIBLE", "ALL", "NONE")
PAGES_PANE_WIDTH = (360, 640)
ORDER_COLUMN_WIDTHS = {"sheet": (150, 380), "folder": (180, 420)}
ORDER_HINT = "Click Print to flag. Drag rows to reorder.\nCtrl/Shift selects multiple. Hover for full text."

SOURCE_PDF_HELP = """**QUICK START**
1. Select **Source PDF** to keep the original drawing's vector lines and
   searchable text. Your takeoff and interactive legend are added over it.
2. Choose the **Source project folder containing Plans & Docs** with Browse.
   You can then save the exported PDF **in any destination folder**.
   **Leave the source field blank** only when saving the export in that same
   project root. Use export folder clears a previously chosen source folder.
3. Use **individual sheet PDFs named by sheet ID**, or enable **Match drawing
   sets by page labels** for a labeled multi-page set.
4. Click **Check selected pages**. Fix any listed issues before exporting.
   **All checked pages are checked, including pages hidden by search.**
5. Use **Preview PDF** to review the finished pages, then **Save PDF**.
   Source PDF previews ask for a source folder if the field is blank.

**RECOMMENDED: ONE PDF PER SHEET**
Extract drawing sets into separate, single-page PDFs before importing them
into PlanSwift. Name each file by its **sheet ID**, and use the same ID as the
PlanSwift page name. Descriptions after | are allowed:
    A-101 | First floor  ->  A-101.pdf
**Leave page-label matching unchecked** when using individual sheet files.

**FOLDER STRUCTURE MUST MATCH PLANSWIFT**
Mirror the Pages panel folders under the chosen source project folder:
    Project root / Plans & Docs / 2026-01-01 / Permit set / A-001.pdf
Choose **Project root**, not Plans & Docs or the final Permit set subfolder.
Keep **the same drawing revision** and folder organization. The chosen source
folder is remembered **for this project**.

**OPTION: USE A LABELED DRAWING SET DIRECTLY**
Enable **Match drawing sets by page labels**. No extraction is needed.
**Every page must have a real PDF page label matching its sheet ID**, such as
A-001 or A-101. Matching ignores case and outside spaces. Printed sheet names,
bookmarks, and unlabeled page positions are not used. Set labels in your PDF
editor first; use **Check PDF labels...** below to inspect them.

Put the labeled set inside its matching document folder:
    Project root / Plans & Docs / 2026-01-01 / Permit set / Drawings.pdf
Or beside a same-named PlanSwift document folder:
    Project root / Plans & Docs / 2026-01-01 / Permit set.pdf
**Each selected sheet must match exactly once.** Keep alternate revisions in
separate folders. Keep only readable, fully labeled PDFs in that location.

**WHAT THE EXPORTER CHECKS**
Check selected pages verifies the selected source sheets. With a blank source
field, it asks you to choose the source project folder to check.
**Next: Save PDF checks a chosen source folder again.** If you use the export
folder, checking runs after the destination is chosen, before PDF creation.
The final check also verifies alignment against PlanSwift's TIFF dimensions.
**Missing, duplicate, unreadable, or password-protected sources stop the whole
export.** Fix the listed sheets and retry. An existing output PDF is preserved;
the exporter does not substitute raster pages or export an incomplete set.

**ALIGNMENT REQUIREMENTS**
Use the **same revision, crop, and orientation** imported into PlanSwift.
The **PlanSwift TIFF is still required** to position takeoff accurately.
Different page proportions are rejected. Original source files are preserved.
"""

MARK_PAGES_HELP = """IN THIS WINDOW
Click a page's Print cell in Selected pages - PDF order to flag or unflag
it. A printer icon means it is flagged; a blank cell means it is not.
This leaves its selection and PDF order unchanged.

FOR SEVERAL PAGES AT ONCE
1. Check the pages you want to flag. You can select several pages or folders.
2. Open Print flags and choose Flag checked pages or Unflag checked pages.
   This includes checked pages hidden by the current search.
3. Your flags are saved when you close the window, including Cancel or X.
   Next: Save PDF also saves them before opening the PDF destination dialog.
   Save flags only & close is available when you only want to manage flags.

Flagged pages show a PDF printer icon in both page lists. Clear all print
flags removes every flag in the project. Undo unsaved flag changes restores
the original flags. To discard flag edits, use Undo unsaved flag changes
before closing. Cancel stops the PDF export but keeps your print flags.

Select only flagged pages selects just those drawings. Takeoff + flagged
pages also includes pages with takeoff. Both shortcuts replace the current
selection and reset PDF order to PlanSwift order. Unchecking a page for
this export leaves its saved flag in place.

FROM PLANSWIFT (ONE PAGE AT A TIME)
Select a page in PlanSwift's Pages panel. Hold Ctrl while clicking this
Export Takeoff to PDF plugin, then release Ctrl. The page is flagged and
the export window stays closed. Repeat on the same page to unflag it.
Run the plugin normally when you are ready to export.

Use Ctrl with this plugin, not PlanSwift's built-in Print button.
"""


@dataclass(frozen=True)
class Node:
    guid: str
    parent: int
    name: str
    is_page: bool = False
    visible: bool = False
    takeoff: bool = False
    marked: bool = False
    takeoff_path: str = ""


@dataclass(frozen=True)
class Catalog:
    token: str
    project: str
    current: str
    nodes: tuple[Node, ...]


@dataclass(frozen=True)
class ExportPreferences:
    content: str = "VISIBLE"
    legend: bool = True
    greyscale: bool = True
    background: str = "RASTER"
    pdf_labels: bool = False
    source_roots: dict[str, str] = field(default_factory=dict)
    preview_view: str = "Thumbnails"
    legend_defaults: dict = field(default_factory=dict)
    page_items: str = "VISIBLE"


def default_preferences_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "PlanSwift" / "Takeoff PDF Community" / "preferences.json"


def load_preferences(path: Path | None) -> ExportPreferences:
    defaults = ExportPreferences()
    if path is None:
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    values = asdict(defaults)
    for key, default in values.items():
        value = data.get(key)
        if type(value) is type(default):
            if key == "legend_defaults":
                from pdf_legend_defaults import clean_defaults
                values[key] = clean_defaults(value)
                continue
            if key == "source_roots":
                values[key] = {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
                continue
            if key == "page_items" and value not in PAGE_ITEM_MODES:
                continue
            if key == "content" and value not in MODES:
                continue
            if key == "background" and value not in ("RASTER", "PDF"):
                continue
            if key == "preview_view" and value not in ("Thumbnails", "List"):
                continue
            values[key] = value
    return ExportPreferences(**values)


def save_preferences(path: Path, preferences: ExportPreferences):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Separate temporary names allow multiple PlanSwift windows to save safely.
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(json.dumps(asdict(preferences), indent=2) + "\n")
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def read_catalog(path: Path) -> Catalog:
    # Only CR/LF are record separators. Unicode line/paragraph separators
    # can legitimately occur in a folder caption.
    lines = [line.removesuffix("\r") for line in path.read_text(encoding="utf-16").split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    if lines and lines[0] == "PDF_EXPORT_PICKER_REQUEST_V1":
        raise ValueError("Please replace the complete PlanSwift Script Code with the updated "
                         "Export takeoff to pdf - FULL REPLACEMENT.txt, then run the plugin again.")
    if len(lines) < 5 or lines[0] != REQUEST_HEADER or lines[-1] != "END":
        raise ValueError("The page list is incomplete or has an unsupported format.")
    token, project, current = lines[1:4]
    if not token or not token.isascii():
        raise ValueError("Invalid export session.")
    records = lines[4:-1]
    if len(records) % 9:
        raise ValueError("The page list contains an incomplete entry.")
    nodes = []
    page_guids = set()
    for start in range(0, len(records), 9):
        tag, guid, parent, kind, name, visible, takeoff, marked, takeoff_path = records[start:start + 9]
        if tag != "NODE" or kind not in ("PAGE", "FOLDER"):
            raise ValueError("Invalid page entry.")
        if any(flag not in ("0", "1") for flag in (visible, takeoff, marked)):
            raise ValueError("Invalid page flags.")
        parent = int(parent)
        if parent < -1 or parent >= len(nodes) or (parent >= 0 and nodes[parent].is_page):
            raise ValueError("Invalid page folder hierarchy.")
        if kind == "PAGE":
            if not guid or not guid.isascii() or guid.casefold() in page_guids:
                raise ValueError("Missing or duplicate page identity.")
            page_guids.add(guid.casefold())
        nodes.append(Node(guid, parent, name, kind == "PAGE", visible == "1",
                          takeoff == "1", marked == "1", takeoff_path))
    return Catalog(token, project, current, tuple(nodes))


class SelectionModel:
    """Selection is stored by page index, never by caption or visible tree row."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.nodes = catalog.nodes
        self.pages = {i for i, n in enumerate(self.nodes) if n.is_page}
        self.original_marks = frozenset(i for i in self.pages if self.nodes[i].marked)
        self.marks = set(self.original_marks)
        self.descendants = [set() for _ in self.nodes]
        self.paths = []
        self.depths = []
        for i, node in enumerate(self.nodes):
            prefix = self.paths[node.parent] + " / " if node.parent >= 0 else ""
            self.paths.append(prefix + node.name)
            self.depths.append(self.depths[node.parent] + 1 if node.parent >= 0 else 0)
            if node.is_page:
                cursor = i
                while cursor >= 0:
                    self.descendants[cursor].add(i)
                    cursor = self.nodes[cursor].parent
        self.mode = "VISIBLE"
        self.query = ""
        self.edited = False
        self.checked = set()
        self.order = []
        self.custom_order = False
        self.matches = set(self.pages)
        self.shown = set(range(len(self.nodes)))
        self.current = next((i for i in self.pages
                             if self.nodes[i].guid.casefold() == catalog.current.casefold()), None)
        self.select_defaults()

    def select_defaults(self):
        self.checked = {i for i in self.pages if i in self.marks or
                        (self.nodes[i].visible if self.mode == "VISIBLE" else self.nodes[i].takeoff)}
        self.edited = False
        self.custom_order = False
        self.order = sorted(self.checked)

    def set_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError("Invalid content mode.")
        self.mode = mode
        if not self.edited:
            self.select_defaults()

    def filter(self, query: str):
        self.query = query.strip().casefold()
        words = self.query.split()
        self.matches = {i for i in self.pages
                        if all(word in self.paths[i].casefold() for word in words)}
        if not words:
            self.shown = set(range(len(self.nodes)))
        else:
            self.shown = set(self.matches)
            for i in self.matches:
                parent = self.nodes[i].parent
                while parent >= 0:
                    self.shown.add(parent)
                    parent = self.nodes[parent].parent

    def targets(self, index: int) -> set[int]:
        return self.descendants[index] & self.matches

    def state(self, index: int) -> str:
        targets = self.targets(index)
        chosen = targets & self.checked
        if not chosen:
            return "off"
        return "on" if chosen == targets else "mixed"

    def toggle(self, index: int):
        targets = self.targets(index)
        self.choose(targets, not targets.issubset(self.checked))

    def choose(self, targets: set[int], checked: bool):
        if checked:
            self.checked.update(targets)
        else:
            self.checked.difference_update(targets)
        self.sync_order()
        self.edited = True

    def select_current(self):
        if self.current is not None:
            self.checked = {self.current}
            self.order = [self.current]
            self.custom_order = False
            self.edited = True

    def selected_guids(self) -> list[str]:
        return [self.nodes[i].guid for i in self.order]

    def sync_order(self):
        if not self.custom_order:
            self.order = sorted(self.checked)
        else:
            self.order = [i for i in self.order if i in self.checked]
            self.order.extend(sorted(self.checked - set(self.order)))

    def move_order(self, indices, position):
        """Insert selected rows at a boundary in the original ordered list."""
        moving = [i for i in self.order if i in indices]
        if not moving:
            return
        position = max(0, min(position, len(self.order)))
        insert_at = sum(i not in indices for i in self.order[:position])
        remaining = [i for i in self.order if i not in indices]
        self.order = remaining[:insert_at] + moving + remaining[insert_at:]
        self.custom_order = self.edited = True

    def shift_order(self, indices, step):
        positions = range(1, len(self.order)) if step < 0 else range(len(self.order) - 2, -1, -1)
        for pos in positions:
            neighbor = pos + (-1 if step < 0 else 1)
            if self.order[pos] in indices and self.order[neighbor] not in indices:
                self.order[pos], self.order[neighbor] = self.order[neighbor], self.order[pos]
        self.custom_order = self.edited = True

    def sort_order(self, mode):
        def natural(text):
            return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                         for part in re.split(r'(\d+)', text))

        def sheet(index):
            name = self.nodes[index].name.strip().lstrip('*').split('|', 1)[0].strip()
            return natural(name), index

        if mode == 'project':
            self.order = sorted(self.checked)
        elif mode == 'reverse':
            self.order.reverse()
        elif mode in ('sheet', 'sheet_desc'):
            self.order.sort(key=sheet, reverse=mode == 'sheet_desc')
        elif mode == 'folder':
            self.order.sort(key=lambda i: (natural(self.paths[self.nodes[i].parent])
                                          if self.nodes[i].parent >= 0 else (), sheet(i)))
        else:
            raise ValueError('Unknown page ordering.')
        self.custom_order = mode != 'project'
        self.edited = True

    def set_marks(self, indices, marked):
        if not set(indices).issubset(self.pages):
            raise ValueError('Only pages can have print flags.')
        if marked:
            self.marks.update(indices)
        else:
            self.marks.difference_update(indices)

    def mark_changes(self):
        return [(self.nodes[i].guid, i in self.marks) for i in sorted(self.original_marks ^ self.marks)]

    def select_marked(self):
        self.checked = set(self.marks)
        self.order = sorted(self.checked)
        self.custom_order = False
        self.edited = True


def write_result(path: Path, model: SelectionModel, legend: bool, greyscale: bool,
                 background: str, action: str = "EXPORT", source_root: str = "", page_items: str = "VISIBLE"):
    if action not in ("EXPORT", "PREVIEW", "MARKS_ONLY"):
        raise ValueError("Invalid window action.")
    if (not model.checked and action != "MARKS_ONLY") or not model.checked.issubset(model.pages):
        raise ValueError("Select at least one page.")
    if len(model.order) != len(model.checked) or set(model.order) != model.checked:
        raise ValueError("The page order does not match the selected pages.")
    if background not in ("RASTER", "PDF", "PDF_LABELS") or model.mode not in MODES:
        raise ValueError("Invalid export options.")
    if page_items not in PAGE_ITEM_MODES:
        raise ValueError("Invalid page item mode.")
    lines = [RESULT_HEADER, model.catalog.token, model.mode,
             "1" if legend and model.mode != "DRAWING" else "0",
             "1" if greyscale else "0", background, action,
             json.dumps(source_root if background != "RASTER" and action != "MARKS_ONLY" else "", ensure_ascii=True),
             page_items, "PAGES", *model.selected_guids(), "MARKS"]
    for guid, marked in model.mark_changes():
        lines.extend([guid, "1" if marked else "0"])
    lines.append("END")
    payload = ("\r\n".join(lines) + "\r\n").encode("ascii")
    pending = path.with_suffix(path.suffix + ".pending")
    try:
        pending.write_bytes(payload)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def read_session_state(path):
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_session_state(path, data):
    path = Path(path)
    pending = path.with_suffix(".pending")
    try:
        pending.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


class PickerWindow:
    def __init__(self, catalog: Catalog, result_path: Path | None = None,
                 preferences_path: Path | None = None, state_path: Path | None = None):
        import tkinter as tk
        from tkinter import ttk, font

        self.tk, self.ttk = tk, ttk
        self.model = SelectionModel(catalog)
        self.result_path = result_path
        self.state_path = state_path
        # Demo windows do not read or change the user's saved preferences.
        self.preferences_path = preferences_path if preferences_path is not None else (
            default_preferences_path() if result_path is not None else None)
        preferences = load_preferences(self.preferences_path)
        self.source_project_key = catalog.nodes[0].guid if catalog.nodes else ""
        self.model.set_mode(preferences.content)
        self.legend_defaults = preferences.legend_defaults
        self.accepted = False
        self.root = tk.Tk()
        self.root.title("Create Takeoff PDF")
        window_width = min(1400, self.root.winfo_screenwidth() - 80)
        window_height = min(900, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(940, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.root.bind("<Escape>", lambda event: self.cancel())
        self.search_job = None
        self.layout_job = None
        self.cell_tip = self.cell_tip_job = self.cell_tip_target = None
        self.normal_open = {i for i, n in enumerate(catalog.nodes) if not n.is_page}
        self.rendered_query = ""
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Hint.TLabel", foreground="#526276")
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=(8, 5))
        style.configure("Help.TButton", padding=3)
        self.help_icon = self.make_help_icon()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        heading = ttk.Frame(self.root, padding=(24, 18, 24, 12))
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="Takeoff PDF", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text=catalog.project or "Current PlanSwift project",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 0))
        body = ttk.Frame(self.root, padding=(24, 0, 24, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        # The settings need their own scroll area: fixed-height packing used
        # to cut off the final shortcut hint on shorter screens / larger fonts.
        self.settings_frame = ttk.Frame(body)
        self.settings_frame.grid(row=0, column=0, sticky="ns", padx=(0, 18))
        self.settings_frame.rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(self.settings_frame, width=290,
                                         borderwidth=0, highlightthickness=0,
                                         background=style.lookup("TFrame", "background"))
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        self.settings_scrollbar = ttk.Scrollbar(self.settings_frame, orient="vertical",
                                                command=self.settings_canvas.yview)
        # Allocate three times the theme's normal width. Stretch the themed
        # scrollbar itself so its thumb and track share the larger hit area.
        self.settings_frame.columnconfigure(1, minsize=self.settings_scrollbar.winfo_reqwidth() * 3)
        self.settings_scrollbar.grid(row=0, column=1, sticky="nsew")
        self.settings_canvas.configure(yscrollcommand=self.settings_scrollbar.set)
        left = ttk.Frame(self.settings_canvas, padding=(0, 0, 6, 8))
        self.settings_content = left
        self.settings_window = self.settings_canvas.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>", self.update_settings_scroll)
        self.settings_canvas.bind("<Configure>", self.resize_settings)
        self.mode = tk.StringVar(value=preferences.content)
        content = ttk.LabelFrame(left, text="Takeoff items", padding=12)
        content.pack(fill="x", pady=(0, 14))
        for label, value in (("Visible items", "VISIBLE"), ("All items", "ALL"),
                             ("None", "DRAWING")):
            ttk.Radiobutton(content, text=label, value=value, variable=self.mode,
                            command=self.change_mode).pack(anchor="w", pady=5)
        self.page_items = tk.StringVar(value=preferences.page_items)
        page_content = ttk.LabelFrame(left, text="Page items", padding=12)
        page_content.pack(fill="x", pady=(0, 14))
        ttk.Label(page_content, text="Clouds, stamps, sticky tabs, notes,\nhighlighters and dimensions.",
                  style="Hint.TLabel", wraplength=235).pack(anchor="w", pady=(0, 5))
        for label, value in (("Visible items", "VISIBLE"), ("All items", "ALL"), ("None", "NONE")):
            ttk.Radiobutton(page_content, text=label, value=value,
                            variable=self.page_items).pack(anchor="w", pady=5)
        ttk.Label(page_content, text="Background only: choose None\nin both groups.",
                  style="Hint.TLabel", wraplength=235).pack(anchor="w", pady=(5, 0))
        options = ttk.LabelFrame(left, text="PDF options", padding=12)
        options.pack(fill="x", pady=(0, 14))
        self.legend = tk.BooleanVar(value=preferences.legend)
        self.greyscale = tk.BooleanVar(value=preferences.greyscale)
        self.legend_check = ttk.Checkbutton(options, text="Include takeoff legend", variable=self.legend)
        self.legend_check.pack(anchor="w", pady=5)
        ttk.Button(options, text="Legend defaults...", command=self.edit_legend_defaults).pack(fill="x", pady=(0, 5))
        if preferences.content == "DRAWING":
            self.legend_check.state(["disabled"])
        ttk.Checkbutton(options, text="Greyscale drawing background",
                        variable=self.greyscale).pack(anchor="w", pady=5)
        background = ttk.LabelFrame(left, text="Drawing background", padding=12)
        background.pack(fill="x", pady=(0, 14))
        self.background = tk.StringVar(value=preferences.background)
        ttk.Radiobutton(background, text="PlanSwift raster (TIFF)", value="RASTER",
                        variable=self.background, command=self.change_background).pack(anchor="w", pady=5)
        source_row = ttk.Frame(background)
        source_row.pack(fill="x", pady=5)
        ttk.Radiobutton(source_row, text="Source PDF", value="PDF", variable=self.background,
                        command=self.change_background).pack(side="left")
        self.source_help_button = ttk.Button(source_row, text="Source PDF help", image=self.help_icon,
                                             compound="image", style="Help.TButton", command=self.show_source_help)
        self.source_help_button.pack(side="left", padx=(6, 0))
        self.source_help_button.bind("<Enter>", self.source_help_hint)
        self.source_help_button.bind("<Leave>", self.hide_cell_tip)
        self.source_help_button.bind("<ButtonPress-1>", self.hide_cell_tip)
        self.pdf_labels = tk.BooleanVar(value=preferences.pdf_labels)
        self.pdf_labels_check = ttk.Checkbutton(background, text="Match drawing sets\nby page labels",
                                                variable=self.pdf_labels)
        self.pdf_labels_check.pack(anchor="w", pady=(5, 0))
        self.source_options = ttk.Frame(background)
        ttk.Label(self.source_options, text="Source project folder").pack(anchor="w", pady=(8, 4))
        self.source_root = tk.StringVar(value=preferences.source_roots.get(self.source_project_key, ""))
        source_path_row = ttk.Frame(self.source_options)
        source_path_row.pack(fill="x")
        self.source_root_entry = ttk.Entry(source_path_row, textvariable=self.source_root, width=16)
        self.source_root_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(source_path_row, text="Browse...", command=self.browse_source_root).pack(side="right", padx=(4, 0))
        ttk.Button(self.source_options, text="Use export folder", command=lambda: self.source_root.set("")).pack(fill="x", pady=(4, 0))
        ttk.Label(self.source_options, text="Choose the folder containing Plans & Docs.\nBlank uses the export folder.",
                  style="Hint.TLabel", wraplength=242).pack(anchor="w", pady=(5, 4))
        self.source_check_button = ttk.Button(self.source_options, text="Check selected pages", command=self.check_selected_sources)
        self.source_check_button.pack(fill="x")
        self.source_check_status = tk.StringVar()
        ttk.Label(self.source_options, textvariable=self.source_check_status, style="Hint.TLabel", wraplength=242).pack(anchor="w")
        self.source_check_signature = None
        self.source_root.trace_add("write", self.reset_source_check)
        self.pdf_labels.trace_add("write", self.reset_source_check)
        self.change_background()
        shortcuts = ttk.LabelFrame(left, text="Page shortcuts", padding=12)
        shortcuts.pack(fill="x")
        self.current_button = ttk.Button(shortcuts, text="Current page", command=self.select_current)
        self.current_button.pack(fill="x", pady=(0, 6))
        if self.model.current is None:
            self.current_button.state(["disabled"])
        ttk.Button(shortcuts, text="Takeoff + flagged pages", command=self.select_defaults).pack(fill="x")
        self.mark_help_button = ttk.Button(shortcuts, text="How to flag pages...", image=self.help_icon,
                                           compound="left", command=self.show_mark_help)
        self.mark_help_button.pack(fill="x", pady=(6, 0))
        marks_button = ttk.Menubutton(shortcuts, text="Print flags")
        marks_menu = tk.Menu(marks_button, tearoff=False)
        for caption, command in (
            ("Flag checked pages", lambda: self.change_marks(self.model.checked, True)),
            ("Unflag checked pages", lambda: self.change_marks(self.model.checked, False)),
            ("Clear all print flags", lambda: self.change_marks(self.model.pages, False)),
            ("Undo unsaved flag changes", self.undo_mark_changes),
            ("Select only flagged pages", self.select_marked_pages),
            ("Save flags only & close", self.save_marks_only),
        ):
            marks_menu.add_command(label=caption, command=command)
        marks_button.configure(menu=marks_menu)
        marks_button.pack(fill="x", pady=(6, 0))
        self.marks_status = tk.StringVar()
        ttk.Label(shortcuts, textvariable=self.marks_status, style="Hint.TLabel",
                  wraplength=242).pack(anchor="w", pady=(6, 0))
        self.shortcut_hint = ttk.Label(shortcuts, text="These shortcuts replace all\nselected pages.",
                                      style="Hint.TLabel", wraplength=242)
        self.shortcut_hint.pack(anchor="w", pady=(8, 0))
        self.bind_settings_scroll(left)

        # Wide windows show the two lists side by side. Narrow windows stack
        # them; users can drag the divider to choose each list's share.
        self.pane_orientation = "horizontal" if window_width >= 1240 else "vertical"
        page_panes = tk.PanedWindow(body, orient=self.pane_orientation, borderwidth=0,
                                    sashwidth=8, sashrelief="raised",
                                    background=style.lookup("TFrame", "background"))
        self.page_panes = page_panes
        page_panes.grid(row=0, column=1, sticky="nsew")
        right = ttk.LabelFrame(page_panes, text="Pages", padding=8)
        self.pages_panel = right
        page_panes.add(right, stretch="always")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        searchrow = ttk.Frame(right)
        searchrow.grid(row=0, column=0, sticky="ew")
        searchrow.columnconfigure(1, weight=1)
        ttk.Label(searchrow, text="Search").grid(row=0, column=0, padx=(0, 8))
        self.search = tk.StringVar()
        self.search_entry = ttk.Entry(searchrow, textvariable=self.search)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(searchrow, text="Clear", command=lambda: self.search.set("")).grid(row=0, column=2, padx=(8, 0))
        self.search.trace_add("write", self.schedule_search)
        actions = ttk.Frame(right)
        actions.grid(row=1, column=0, sticky="ew", pady=5)
        self.select_button = ttk.Button(actions, text="Select all", command=lambda: self.select_matching(True))
        self.select_button.pack(side="left")
        self.clear_button = ttk.Button(actions, text="Clear all", command=lambda: self.select_matching(False))
        self.clear_button.pack(side="left", padx=5)
        self.collapse_button = ttk.Button(actions, text="Collapse", command=lambda: self.expand(False))
        self.collapse_button.pack(side="right")
        self.expand_button = ttk.Button(actions, text="Expand", command=lambda: self.expand(True))
        self.expand_button.pack(side="right", padx=5)
        treeframe = ttk.Frame(right)
        treeframe.grid(row=2, column=0, sticky="nsew")
        treeframe.columnconfigure(0, weight=1)
        treeframe.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(treeframe, show="tree", selectmode="browse", takefocus=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(treeframe, orient="vertical", command=self.tree.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(treeframe, orient="horizontal", command=self.tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        printer = tk.PhotoImage(master=self.root, file=str(Path(__file__).resolve().parent / "assets" / "print_flag.png"))
        self.print_flag_icon = printer.subsample(2)  # Original 42 x 38 PNG, displayed at 21 x 19.
        self.icons = {(kind, state): self.make_icon(kind, state)
                      for kind in ("page", "folder", "print") for state in ("on", "off", "mixed")}
        self.tree.bind("<Button-1>", self.click)
        self.tree.bind("<Double-1>", self.double_click)
        self.tree.bind("<space>", self.space)
        self.empty_label = ttk.Label(treeframe, text="No matching pages", style="Hint.TLabel")
        self.status = tk.StringVar()
        self.selection_hint = tk.StringVar()
        ttk.Label(right, textvariable=self.status).grid(row=3, column=0, sticky="w", pady=(6, 2))
        self.selection_hint_label = ttk.Label(right, textvariable=self.selection_hint, style="Hint.TLabel")
        self.selection_hint_label.grid(row=4, column=0, sticky="w")
        right.bind("<Configure>", lambda event: self.selection_hint_label.configure(wraplength=max(100, event.width - 28)))
        self.build_order_panel(page_panes)
        footer = ttk.Frame(self.root, padding=(24, 16, 24, 18))
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(footer, text="Flags and options are kept on close.",
                  style="Hint.TLabel").pack(side="left")
        self.export_button = ttk.Button(footer, text="Next: Save PDF…", command=self.accept)
        self.export_button.pack(side="right")
        self.preview_button = ttk.Button(footer, text="Preview PDF", command=lambda: self.accept("PREVIEW"))
        self.preview_button.pack(side="right", padx=(8, 0))
        self.legend_defaults_button = ttk.Button(footer, text="Legend defaults…", command=self.edit_legend_defaults)
        self.legend_defaults_button.pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="Cancel", command=self.cancel).pack(side="right", padx=10)
        self.tree_font = font.Font(family="Segoe UI", size=10)
        self.root.bind("<FocusOut>", self.hide_cell_tip, add="+")
        self.root.bind("<Unmap>", self.hide_cell_tip, add="+")
        for tree in (self.tree, self.order_tree):
            tree.bind("<Motion>", self.hover_cell, add="+")
            for event in ("<Leave>", "<MouseWheel>", "<KeyPress>", "<Configure>"):
                tree.bind(event, self.hide_cell_tip, add="+")
        self.restore_session()
        self.render()
        self.search_entry.focus_set()
        self.root.bind("<Configure>", self.resize_page_panes, add="+")
        self.page_panes.bind("<Configure>", self.schedule_page_balance)
        self.schedule_page_balance()

    def schedule_page_balance(self, event=None):
        if self.layout_job is not None:
            self.root.after_cancel(self.layout_job)
        self.layout_job = self.root.after_idle(self.balance_page_panes)

    def resize_page_panes(self, event):
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if size == getattr(self, "last_pane_size", None):
            return
        self.last_pane_size = size
        orient = "horizontal" if event.width >= 1240 else "vertical"
        if orient != self.pane_orientation:
            self.pane_orientation = orient
            self.page_panes.configure(orient=orient)
        self.update_selection_labels()
        self.schedule_page_balance()

    def balance_page_panes(self):
        self.layout_job = None
        size = self.page_panes.winfo_width() if self.pane_orientation == "horizontal" else self.page_panes.winfo_height()
        if self.pane_orientation == "horizontal":
            # Reserve enough room for the selected-page toolbar even when a
            # drawing name is unusually long. The user can still drag the sash.
            position = max(PAGES_PANE_WIDTH[0], min(self.pages_panel_width, size - 408))
            self.page_panes.sash_place(0, position, 0)
        else:
            # In a narrow window both panels use the available width; reserve
            # height for the order toolbar and at least two visible rows.
            self.page_panes.sash_place(0, 0, int(size * 0.44))

    def resize_settings(self, event):
        self.settings_canvas.itemconfigure(self.settings_window, width=event.width)

    def build_order_panel(self, panes):
        panel = self.ttk.LabelFrame(panes, text="Selected pages - PDF order", padding=10)
        self.order_panel = panel
        panes.add(panel, stretch="always")
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)
        toolbar = self.ttk.Frame(panel)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        moves = self.ttk.Frame(toolbar)
        moves.pack(fill="x", pady=(0, 4))
        for label, command in (("Top", lambda: self.move_selected_to(0)),
                               ("Up", lambda: self.shift_selected(-1)),
                               ("Down", lambda: self.shift_selected(1)),
                               ("Bottom", lambda: self.move_selected_to(len(self.model.order)))):
            self.ttk.Button(moves, text=label, width=6, command=command).pack(side="left", padx=(0, 4))
        self.ttk.Button(moves, text="Remove", command=self.remove_selected_order_rows).pack(side="right")
        sorting = self.ttk.Frame(toolbar)
        sorting.pack(fill="x")
        sort_button = self.ttk.Menubutton(sorting, text="Sort order")
        menu = self.tk.Menu(sort_button, tearoff=False)
        for label, mode in (("PlanSwift order (default)", "project"), ("Sheet ID: A to Z", "sheet"),
                            ("Sheet ID: Z to A", "sheet_desc"), ("Folder, then sheet ID", "folder"),
                            ("Reverse current order", "reverse")):
            menu.add_command(label=label, command=lambda value=mode: self.sort_selected(value))
        sort_button.configure(menu=menu)
        sort_button.pack(side="left", padx=4)
        self.restore_order_button = self.ttk.Button(sorting, text="Restore default order",
                                                    command=lambda: self.sort_selected("project"))
        self.restore_order_button.pack(side="right")
        area = self.ttk.Frame(panel)
        area.grid(row=1, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.order_tree = self.ttk.Treeview(area, columns=("number", "sheet", "folder"),
                                           show="tree headings", height=6, selectmode="extended")
        self.order_tree.heading("#0", text="Print")
        self.order_tree.column("#0", width=48, minwidth=48, stretch=False)
        for name, title, width in (("number", "#", 42), ("sheet", "Sheet", 260), ("folder", "Folder", 220)):
            self.order_tree.heading(name, text=title)
            self.order_tree.column(name, width=width, minwidth=ORDER_COLUMN_WIDTHS.get(name, (width, width))[0],
                                   stretch=False, anchor="center" if name == "number" else "w")
        self.order_tree.grid(row=0, column=0, sticky="nsew")
        bar = self.ttk.Scrollbar(area, orient="vertical", command=self.order_tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        xbar = self.ttk.Scrollbar(area, orient="horizontal", command=self.order_tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.order_tree.configure(yscrollcommand=lambda *args: self.scroll_order_view(bar, *args),
                                  xscrollcommand=lambda *args: self.scroll_order_view(xbar, *args))
        self.order_hint = self.tk.StringVar(value=ORDER_HINT)
        self.order_hint_label = self.ttk.Label(panel, textvariable=self.order_hint, style="Hint.TLabel")
        self.order_hint_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        panel.bind("<Configure>", lambda event: self.order_hint_label.configure(wraplength=max(100, event.width - 24)))
        self.order_tree.bind("<ButtonPress-1>", self.order_drag_start)
        self.order_tree.bind("<B1-Motion>", self.order_drag_motion)
        self.order_tree.bind("<ButtonRelease-1>", self.order_drag_end)
        self.order_tree.bind("<Alt-Up>", lambda event: (self.shift_selected(-1), "break")[1])
        self.order_tree.bind("<Alt-Down>", lambda event: (self.shift_selected(1), "break")[1])
        self.order_tree.bind("<Delete>", lambda event: (self.remove_selected_order_rows(), "break")[1])
        self.drop_line = self.tk.Frame(self.order_tree, background="#1672c4", height=2)
        self.drag_row = None
        self.drag_position = None
        self.flag_click = self.resizing_order_column = False
        self.sized_order_pages = None

    def scroll_order_view(self, scrollbar, *args):
        scrollbar.set(*args)
        self.hide_cell_tip()

    def refresh_order(self):
        self.hide_cell_tip()
        selection = self.order_tree.selection()
        focus = self.order_tree.focus()
        scroll = self.order_tree.yview()
        rows = self.order_tree.get_children()
        if rows:
            self.order_tree.delete(*rows)
        for number, index in enumerate(self.model.order, 1):
            node = self.model.nodes[index]
            folder = self.model.paths[node.parent] if node.parent >= 0 else ""
            self.order_tree.insert("", "end", iid=str(index), values=(number, self.page_caption(index), folder),
                                   image=self.print_flag_icon if index in self.model.marks else "")
        self.order_tree.selection_set([i for i in selection if self.order_tree.exists(i)])
        if focus and self.order_tree.exists(focus):
            self.order_tree.focus(focus)
        if scroll:
            self.order_tree.yview_moveto(scroll[0])
        # Flags and reordering do not alter text widths. Leave any manual
        # column adjustments alone until the set of selected pages changes.
        pages = frozenset(self.model.order)
        if pages != self.sized_order_pages:
            for column, (minimum, maximum) in ORDER_COLUMN_WIDTHS.items():
                width = max((self.tree_font.measure(self.order_tree.set(str(i), column)) + 24
                             for i in pages), default=minimum)
                self.order_tree.column(column, width=max(minimum, min(maximum, width)))
            self.sized_order_pages = pages

    def hover_cell(self, event):
        tree = event.widget
        row, column = tree.identify_row(event.y), tree.identify_column(event.x)
        if not row or event.state & 256 or tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            self.hide_cell_tip()
            return
        text = ""
        is_print = tree is self.order_tree and column == "#0"
        if is_print:
            text = "Click to remove print flag" if int(row) in self.model.marks else "Click to flag for printing"
        elif tree is self.order_tree and column in ("#2", "#3"):
            text = tree.set(row, "sheet" if column == "#2" else "folder")
        elif tree is self.tree:
            text = self.page_caption(int(row))
        if not text:
            self.hide_cell_tip()
            return
        x, y, width, height = tree.bbox(row, column)
        start = x + (self.model.depths[int(row)] * 20 + 68 if tree is self.tree else 4)
        clipped = start < 0 or self.tree_font.measure(text) + start + 8 > min(x + width, tree.winfo_width())
        if not is_print and not clipped:
            self.hide_cell_tip()
            return
        target = (tree, row, column, text)
        if target == self.cell_tip_target:
            return
        self.hide_cell_tip()
        tree.configure(cursor="hand2" if is_print else "")
        self.cell_tip_target = target
        self.cell_tip_position = (event.x_root + 12, event.y_root + 20)
        self.cell_tip_job = self.root.after(500, self.show_cell_tip)

    def source_help_hint(self, event):
        self.hide_cell_tip()
        self.cell_tip_target = (event.widget, "", "", "Source PDF: setup requirements and page matching")
        self.cell_tip_position = (event.x_root + 12, event.y_root + 20)
        self.cell_tip_job = self.root.after(500, self.show_cell_tip)

    def show_cell_tip(self):
        self.cell_tip_job = None
        if self.cell_tip_target is None:
            return
        tip = self.tk.Toplevel(self.root)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        self.cell_tip = tip
        self.cell_tip_label = self.tk.Label(tip, text=self.cell_tip_target[3], justify="left",
                                            font=("Segoe UI", 10), background="#fffde7", foreground="#17212e",
                                            relief="solid", borderwidth=1, padx=8, pady=6,
                                            wraplength=min(700, self.root.winfo_width() - 40))
        self.cell_tip_label.pack()
        tip.update_idletasks()
        x, y = self.cell_tip_position
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        x = max(left, min(x, left + self.root.winfo_width() - tip.winfo_reqwidth()))
        if y + tip.winfo_reqheight() > top + self.root.winfo_height():
            y = self.cell_tip_position[1] - tip.winfo_reqheight() - 34
        tip.geometry(f"+{x}+{max(top, y)}")
        tip.deiconify()

    def hide_cell_tip(self, event=None):
        if self.cell_tip_job is not None:
            self.root.after_cancel(self.cell_tip_job)
            self.cell_tip_job = None
        if self.cell_tip is not None:
            self.cell_tip.destroy()
            self.cell_tip = None
        if self.cell_tip_target is not None:
            self.cell_tip_target[0].configure(cursor="")
            self.cell_tip_target = None

    def selected_order_rows(self):
        return {int(i) for i in self.order_tree.selection()}

    def move_selected_to(self, position):
        self.model.move_order(self.selected_order_rows(), position)
        self.refresh_order()

    def shift_selected(self, step):
        self.model.shift_order(self.selected_order_rows(), step)
        self.refresh_order()
        selected = self.order_tree.selection()
        if selected:
            self.order_tree.see(selected[0] if step < 0 else selected[-1])

    def sort_selected(self, mode):
        self.model.sort_order(mode)
        self.refresh_order()

    def remove_selected_order_rows(self):
        self.model.choose(self.selected_order_rows(), False)
        self.refresh_selection()

    def order_drag_start(self, event):
        self.hide_cell_tip()
        region = self.order_tree.identify_region(event.x, event.y)
        self.resizing_order_column = region == "separator"
        self.flag_click = False
        self.drag_row = self.order_tree.identify_row(event.y)
        self.drag_y = event.y
        self.drag_position = None
        if self.resizing_order_column or region not in ("tree", "cell"):
            self.drag_row = None
            return
        if self.drag_row and self.order_tree.identify_column(event.x) == "#0":
            index = int(self.drag_row)
            self.drag_row = None
            self.flag_click = True
            self.change_marks({index}, index not in self.model.marks)
            return "break"
        if self.drag_row and self.drag_row in self.order_tree.selection() and not event.state & 5:
            self.order_tree.focus(self.drag_row)
            self.order_tree.focus_set()
            return "break"  # preserve a multi-row selection while dragging

    def order_drag_motion(self, event):
        if not self.drag_row or abs(event.y - self.drag_y) < 4:
            return
        row = self.order_tree.identify_row(event.y)
        if row:
            _, y, _, height = self.order_tree.bbox(row)
            after = event.y >= y + height / 2
            self.drag_position = self.model.order.index(int(row)) + int(after)
            line_y = y + height if after else y
        else:
            self.drag_position = 0 if event.y < 25 else len(self.model.order)
            line_y = max(24, min(event.y, self.order_tree.winfo_height() - 2))
        self.drop_line.place(x=0, y=line_y, relwidth=1)
        self.order_hint.set("Release to move selected pages here.")
        if event.y < 35:
            self.order_tree.yview_scroll(-1, "units")
        elif event.y > self.order_tree.winfo_height() - 20:
            self.order_tree.yview_scroll(1, "units")
        return "break"

    def order_drag_end(self, event):
        if self.flag_click:
            self.flag_click = False
            return "break"
        if self.resizing_order_column:
            self.resizing_order_column = False
            for column, (minimum, maximum) in ORDER_COLUMN_WIDTHS.items():
                width = self.order_tree.column(column, "width")
                self.order_tree.column(column, width=max(minimum, min(maximum, width)))
        dragged = self.drag_position is not None
        if dragged:
            self.model.move_order(self.selected_order_rows(), self.drag_position)
            self.refresh_order()
        self.drop_line.place_forget()
        self.drag_row = self.drag_position = None
        self.order_hint.set(ORDER_HINT)
        if dragged:
            return "break"

    def update_settings_scroll(self, event=None):
        self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all"))

    def bind_settings_scroll(self, widget):
        widget.bind("<MouseWheel>", self.scroll_settings, add="+")
        # When tabbing to a setting below the fold, bring it into view.
        widget.bind("<FocusIn>", self.reveal_setting, add="+")
        for child in widget.winfo_children():
            self.bind_settings_scroll(child)
        self.settings_canvas.bind("<MouseWheel>", self.scroll_settings)

    def scroll_settings(self, event):
        if self.settings_content.winfo_height() > self.settings_canvas.winfo_height():
            self.settings_canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def reveal_setting(self, event):
        # Tk sends FocusIn to the focused control AND its ancestor frames.
        # Revealing a whole settings frame scrolls even when the clicked radio
        # is already visible. Only reveal the actual focus owner; this keeps
        # keyboard traversal to offscreen controls working.
        if event.widget != self.root.focus_get():
            return
        canvas = self.settings_canvas
        height = self.settings_content.winfo_height()
        top = event.widget.winfo_rooty() - self.settings_content.winfo_rooty()
        visible_top = canvas.canvasy(0)
        bottom = top + event.widget.winfo_height()
        if top < visible_top:
            canvas.yview_moveto(max(0, top - 8) / max(1, height))
        elif bottom > visible_top + canvas.winfo_height():
            canvas.yview_moveto((bottom + 8 - canvas.winfo_height()) / max(1, height))

    def change_background(self):
        self.pdf_labels_check.state(["!disabled"] if self.background.get() == "PDF" else ["disabled"])
        if self.background.get() == "PDF":
            self.source_options.pack(fill="x")
        else:
            self.source_options.pack_forget()

    def reset_source_check(self, *args):
        self.source_check_signature = None
        self.source_check_status.set("")

    def browse_source_root(self):
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.root, mustexist=True,
                    title="Source project folder (contains Plans & Docs)",
                    initialdir=self.source_root.get() or None)
        if selected:
            self.source_root.set(selected)
        return bool(selected)

    def check_selected_sources(self, show_success=True):
        from tkinter import messagebox
        if not self.model.checked:
            messagebox.showinfo("Check source PDFs", "Select the pages to check first.", parent=self.root)
            return False
        if not self.source_root.get().strip() and not self.browse_source_root():
            return False
        root = os.path.expandvars(os.path.expanduser(self.source_root.get().strip()))
        self.source_root.set(root)
        pages = [{"PageName": self.model.nodes[i].name, "TakeoffPath": self.model.nodes[i].takeoff_path}
                 for i in self.model.order]
        self.root.configure(cursor="watch")
        self.source_check_button.state(["disabled"])
        try:
            from pdf_source_paths import check_source_pages
            def progress(done, total, name):
                self.source_check_status.set(f"Checking {done} of {total}: {name}")
                self.root.update_idletasks()
            matches, issues = check_source_pages(root, pages, self.pdf_labels.get(), progress)
        except Exception as exc:
            matches, issues = [], [("Source check", str(exc))]
        finally:
            self.root.configure(cursor="")
            self.source_check_button.state(["!disabled"])
        self.source_check_signature = (frozenset(self.model.checked), root, self.pdf_labels.get())
        self.source_check_status.set(f"{len(matches)} ready; {len(issues)} need attention.")
        if show_success or issues:
            self.show_source_check_results(matches, issues)
        return not issues

    def show_source_check_results(self, matches, issues):
        from tkinter.scrolledtext import ScrolledText
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Source PDF check")
        dialog.geometry("780x520")
        dialog.transient(self.root)
        summary = f"{len(matches)} pages ready; {len(issues)} need attention."
        lines = [summary, ""]
        if issues:
            lines += ["Export is blocked until these issues are fixed:", ""]
            lines += [f"{name}\n  {reason}\n" for name, reason in issues]
        if matches:
            lines += ["Ready:", ""]
            for name, reference in matches:
                page = reference[1] + 1 if reference[1] is not None else 1
                lines.append(f"{name}\n  {reference[0]} (PDF page {page})\n")
        lines += ["The exporter checks again before creating the PDF, including TIFF alignment."]
        text = ScrolledText(dialog, wrap="word", font=("Segoe UI", 10), padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        self.ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=8)
        dialog.bind("<Escape>", lambda event: (dialog.destroy(), "break")[1])

    def show_mark_help(self):
        from tkinter import messagebox
        messagebox.showinfo("Flag pages for printing", MARK_PAGES_HELP, parent=self.root)

    def page_caption(self, index):
        return self.model.nodes[index].name

    def page_icon(self, index):
        kind = "print" if index in self.model.marks else "page" if self.model.nodes[index].is_page else "folder"
        return self.icons[kind, self.model.state(index)]

    def change_marks(self, indices, marked):
        self.model.set_marks(indices, marked)
        self.refresh_selection()

    def undo_mark_changes(self):
        self.model.marks = set(self.model.original_marks)
        self.refresh_selection()

    def select_marked_pages(self):
        self.model.select_marked()
        self.search.set("")
        self.apply_search()

    def save_marks_only(self):
        self.accept(action="MARKS_ONLY")

    def show_source_help(self):
        self.hide_cell_tip()
        if getattr(self, "help_window", None) is not None and self.help_window.winfo_exists():
            self.help_window.lift()
            return
        self.help_window = self.tk.Toplevel(self.root)
        dialog = self.help_window
        dialog.title("Source PDF - Setup and page matching")
        dialog.geometry("760x680")
        dialog.minsize(550, 420)
        dialog.transient(self.root)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        panel = self.ttk.Frame(dialog, padding=16)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        text = self.tk.Text(panel, wrap="word", font=("Segoe UI", 10), padx=12, pady=12,
                            borderwidth=0, highlightthickness=0)
        text.grid(row=0, column=0, sticky="nsew")
        bar = self.ttk.Scrollbar(panel, command=text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=bar.set)
        text.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        position = 0
        for match in re.finditer(r"\*\*(.+?)\*\*", SOURCE_PDF_HELP, re.DOTALL):
            text.insert("end", SOURCE_PDF_HELP[position:match.start()])
            text.insert("end", match.group(1), "bold")
            position = match.end()
        text.insert("end", SOURCE_PDF_HELP[position:])
        self.source_help_text = text
        text.configure(state="disabled")
        buttons = self.ttk.Frame(dialog, padding=(16, 0, 16, 16))
        buttons.grid(row=1, column=0, sticky="ew")
        self.ttk.Button(buttons, text="Check PDF labels...", command=self.inspect_pdf_labels).pack(side="left")
        self.ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right")
        dialog.bind("<Escape>", lambda event: (dialog.destroy(), "break")[1])

    def inspect_pdf_labels(self):
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(parent=self.help_window, title="Check drawing-set page labels",
                                          filetypes=[("PDF drawings", "*.pdf")])
        if not path:
            return
        try:
            from pdf_source_labels import read_page_labels, label_key
            labels = read_page_labels(path)
        except Exception as exc:
            messagebox.showerror("Cannot match this PDF", str(exc), parent=self.help_window)
            return
        counts = {}
        for label in labels:
            counts[label_key(label)] = counts.get(label_key(label), 0) + 1
        lines = [Path(path).name, "", "PDF page    Page label / sheet ID", ""]
        for i, label in enumerate(labels):
            issue = "  [DUPLICATE]" if counts[label_key(label)] > 1 else ""
            lines.append(f"{i + 1:>8}    {label}{issue}")
        lines += ["", "Match these labels to the PlanSwift sheet IDs before exporting."]
        preview = self.tk.Toplevel(self.help_window)
        preview.title("PDF page labels")
        preview.geometry("650x480")
        preview.transient(self.help_window)
        from tkinter.scrolledtext import ScrolledText
        text = ScrolledText(preview, wrap="word", font=("Consolas", 10), padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        self.ttk.Button(preview, text="Close", command=preview.destroy).pack(pady=8)
        preview.bind("<Escape>", lambda event: (preview.destroy(), "break")[1])

    def make_help_icon(self):
        img = self.tk.PhotoImage(master=self.root, width=24, height=24)
        for y in range(24):
            for x in range(24):
                distance = (x - 11.5) ** 2 + (y - 11.5) ** 2
                if distance <= 121:
                    img.put("#1769a8" if distance < 100 else "#125281", to=(x, y))
        for y, row in enumerate(("01110", "11011", "00011", "00110", "00100", "00000", "00100")):
            for x, pixel in enumerate(row):
                if pixel == "1":
                    img.put("#ffffff", to=(7 + x * 2, 5 + y * 2, 9 + x * 2, 7 + y * 2))
        return img

    def make_icon(self, kind, state):
        img = self.tk.PhotoImage(master=self.root, width=43, height=22)
        # Checkbox plus a compact folder/page symbol, independent of fonts.
        img.put("#63748a", to=(1, 2, 15, 16))
        img.put("#ffffff" if state == "off" else "#1769a8", to=(2, 3, 14, 15))
        if state == "mixed":
            img.put("#ffffff", to=(4, 8, 12, 10))
        elif state == "on":
            for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)):
                img.put("#ffffff", to=(x, y, x + 2, y + 2))
        if kind == "print":
            img.tk.call(str(img), "copy", str(self.print_flag_icon), "-to", 20, 1)
        elif kind == "folder":
            img.put("#bc861c", to=(21, 3, 28, 6))
            img.put("#bc861c", to=(20, 5, 37, 16))
            img.put("#f8d37b", to=(21, 6, 36, 15))
        else:
            img.put("#71839a", to=(23, 2, 35, 16))
            img.put("#ffffff", to=(24, 3, 34, 15))
            for y in (6, 9, 12):
                img.put("#b6c4d4", to=(26, y, 32, y + 1))
        return img

    def schedule_search(self, *args):
        if self.search_job is not None:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(120, self.apply_search)

    def apply_search(self):
        if self.search_job is not None:
            self.root.after_cancel(self.search_job)
            self.search_job = None
        self.model.filter(self.search.get())
        self.render()

    def flush_search(self):
        if self.search_job is not None:
            self.apply_search()

    def render(self):
        self.hide_cell_tip()
        if not self.rendered_query:
            self.normal_open = {i for i in self.normal_open if not self.tree.exists(str(i))}
            self.normal_open.update(i for i, n in enumerate(self.model.nodes)
                                    if not n.is_page and self.tree.exists(str(i)) and self.tree.item(str(i), "open"))
        focus = self.tree.focus()
        roots = self.tree.get_children("")
        if roots:
            self.tree.delete(*roots)
        width = 280
        for i, node in enumerate(self.model.nodes):
            if i not in self.model.shown:
                continue
            parent = str(node.parent) if node.parent >= 0 else ""
            self.tree.insert(parent, "end", iid=str(i), text=" " + self.page_caption(i),
                             image=self.page_icon(i),
                             open=bool(self.model.query) or i in self.normal_open)
            width = max(width, self.tree_font.measure(self.page_caption(i)) + 80 + self.model.depths[i] * 20)
        self.tree.column("#0", width=width, minwidth=width, stretch=True)
        self.pages_panel_width = max(PAGES_PANE_WIDTH[0], min(PAGES_PANE_WIDTH[1], width + 48))
        self.schedule_page_balance()
        if focus and self.tree.exists(focus):
            self.tree.focus(focus)
            self.tree.selection_set(focus)
        self.rendered_query = self.model.query
        if not self.model.matches:
            self.empty_label.configure(text="No matching pages" if self.model.query else "This project has no pages")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty_label.place_forget()
        filtering = bool(self.model.query)
        self.select_button.configure(text="Select results" if filtering else "Select all")
        self.clear_button.configure(text="Clear results" if filtering else "Clear all")
        for button in (self.expand_button, self.collapse_button):
            button.state(["disabled"] if filtering else ["!disabled"])
        self.refresh_selection()

    def refresh_selection(self):
        if self.source_check_signature is not None and self.source_check_signature[0] != frozenset(self.model.checked):
            self.reset_source_check()
        for i in self.model.shown:
            self.tree.item(str(i), text=" " + self.page_caption(i), image=self.page_icon(i))
        changed = len(self.model.mark_changes())
        self.marks_status.set(f"{len(self.model.marks)} print flags · {changed} unsaved changes")
        self.update_selection_labels()
        self.export_button.state(["!disabled"] if self.model.checked else ["disabled"])
        self.preview_button.state(["!disabled"] if self.model.checked else ["disabled"])
        self.refresh_order()

    def update_selection_labels(self):
        selected = len(self.model.checked)
        self.status.set(f"{selected} of {len(self.model.pages)} pages selected")
        if self.model.query:
            hidden = len(self.model.checked - self.model.matches)
            self.selection_hint.set(f"{len(self.model.matches)} matching pages · {hidden} selected outside this search")
            if self.pane_orientation == "vertical":
                self.status.set(f"{selected} selected · {len(self.model.matches)} matching · {hidden} outside search")
        else:
            self.selection_hint.set("Click a checkbox or press Space. Folder checkboxes include their pages.")
        if self.pane_orientation == "vertical":
            self.selection_hint_label.grid_remove()
        else:
            self.selection_hint_label.grid()

    def click(self, event):
        self.hide_cell_tip()
        row = self.tree.identify_row(event.y)
        if row and "image" in self.tree.identify_element(event.x, event.y):
            self.tree.focus(row)
            self.tree.selection_set(row)
            self.tree.focus_set()
            self.model.toggle(int(row))
            self.refresh_selection()
            return "break"

    def double_click(self, event):
        # The first click already changed the checkbox. Keep a double click
        # on the icon from also expanding a folder or toggling a second time.
        if "image" in self.tree.identify_element(event.x, event.y):
            return "break"

    def space(self, event=None):
        row = self.tree.focus()
        if row:
            self.model.toggle(int(row))
            self.refresh_selection()
        return "break"

    def select_matching(self, checked):
        self.flush_search()
        self.model.choose(self.model.matches, checked)
        self.refresh_selection()

    def select_current(self):
        self.model.select_current()
        self.search.set("")
        self.apply_search()
        if self.model.current is not None:
            row = str(self.model.current)
            self.tree.see(row)
            self.tree.focus(row)
            self.tree.selection_set(row)

    def select_defaults(self):
        self.model.select_defaults()
        self.refresh_selection()

    def change_mode(self):
        self.model.set_mode(self.mode.get())
        self.legend_check.state(["disabled"] if self.model.mode == "DRAWING" else ["!disabled"])
        self.refresh_selection()

    def expand(self, value):
        self.flush_search()
        if self.model.query:
            return
        for i, node in enumerate(self.model.nodes):
            if not node.is_page:
                self.tree.item(str(i), open=value)

    def accept(self, action="EXPORT"):
        from tkinter import messagebox
        if not self.model.checked and action != "MARKS_ONLY":
            return
        if self.result_path is None:
            messagebox.showinfo("Preview only", f"{len(self.model.checked)} pages selected.\n"
                                "This preview does not export or change your project.", parent=self.root)
            return
        if action == "PREVIEW" and self.background.get() == "PDF" and not self.source_root.get().strip():
            # Preview has no export destination to use as the source root yet.
            if not self.browse_source_root():
                return
        if action != "MARKS_ONLY" and self.background.get() == "PDF" and self.source_root.get().strip():
            if not self.check_selected_sources(show_success=False):
                return
        try:
            self.save_current_preferences()
            self.save_session()
            background = self.background.get()
            if background == "PDF" and self.pdf_labels.get():
                background = "PDF_LABELS"
            write_result(self.result_path, self.model, self.legend.get(), self.greyscale.get(), background, action,
                         self.source_root.get().strip(), page_items=self.page_items.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not save choices", str(exc), parent=self.root)
            return
        self.accepted = True
        self.close()

    def save_session(self):
        if self.state_path is None:
            return
        write_session_state(self.state_path, {
            "project": self.source_project_key,
            "order": self.model.selected_guids(),
            "default_order": [self.model.nodes[i].guid for i in sorted(self.model.pages)],
            "edited": self.model.edited, "custom_order": self.model.custom_order,
            "search": self.search.get(),
            "options": {"content": self.mode.get(), "page_items": self.page_items.get(),
                        "legend": self.legend.get(),
                        "greyscale": self.greyscale.get(), "background": self.background.get(),
                        "pdf_labels": self.pdf_labels.get(), "source_root": self.source_root.get()}})

    def restore_session(self):
        state = read_session_state(self.state_path)
        if state.get("project") != self.source_project_key:
            return
        order = state.get("order")
        if not isinstance(order, list) or not all(isinstance(guid, str) for guid in order):
            return
        if len(order) != len(set(order)):
            return
        options = state.get("options", {})
        if not isinstance(options, dict):
            return
        mode = options.get("content", self.mode.get())
        if mode in MODES:
            self.mode.set(mode)
            self.model.set_mode(mode)
        if options.get("page_items") in PAGE_ITEM_MODES:
            self.page_items.set(options["page_items"])
        for name in ("legend", "greyscale", "pdf_labels"):
            if type(options.get(name)) is bool:
                getattr(self, name).set(options[name])
        if options.get("background") in ("RASTER", "PDF"):
            self.background.set(options["background"])
        if isinstance(options.get("source_root"), str):
            self.source_root.set(options["source_root"])
        lookup = {self.model.nodes[i].guid: i for i in self.model.pages}
        self.model.order = [lookup[guid] for guid in order if guid in lookup]
        self.model.checked = set(self.model.order)
        self.model.edited = bool(state.get("edited", True))
        self.model.custom_order = bool(state.get("custom_order", True))
        self.legend_check.state(["disabled"] if mode == "DRAWING" else ["!disabled"])
        self.change_background()
        if isinstance(state.get("search"), str):
            self.search.set(state["search"])
            self.model.filter(state["search"])

    def edit_legend_defaults(self):
        from pdf_legend_defaults import LegendDefaultsDialog
        from dataclasses import replace
        current = load_preferences(self.preferences_path)
        def save(value):
            if self.preferences_path is not None:
                latest = load_preferences(self.preferences_path)
                save_preferences(self.preferences_path, replace(latest, legend_defaults=value))
            self.legend_defaults = value
        return LegendDefaultsDialog(self.root, current.legend_defaults if self.preferences_path else self.legend_defaults, save)

    def save_current_preferences(self):
        if self.preferences_path is not None:
            current = load_preferences(self.preferences_path)
            roots = dict(current.source_roots)
            if self.source_project_key:
                if self.source_root.get().strip():
                    roots[self.source_project_key] = self.source_root.get().strip()
                else:
                    roots.pop(self.source_project_key, None)
            save_preferences(self.preferences_path, ExportPreferences(
                content=self.mode.get(), legend=self.legend.get(), greyscale=self.greyscale.get(),
                background=self.background.get(), pdf_labels=self.pdf_labels.get(), source_roots=roots,
                preview_view=current.preview_view, legend_defaults=current.legend_defaults,
                page_items=self.page_items.get()))

    def close(self):
        self.hide_cell_tip()
        if self.layout_job is not None:
            self.root.after_cancel(self.layout_job)
            self.layout_job = None
        if self.search_job is not None:
            self.root.after_cancel(self.search_job)
            self.search_job = None
        self.root.destroy()

    def cancel(self):
        # Return the existing flags-only response so PlanSwift saves the edits
        # and exits before opening its PDF destination dialog. X and Escape
        # use this same path, even if no pages are selected for export.
        if self.result_path is not None and self.model.mark_changes():
            self.accept(action="MARKS_ONLY")
            return
        try:
            self.save_current_preferences()
        except OSError as exc:
            from tkinter import messagebox
            messagebox.showerror("Could not save options", str(exc), parent=self.root)
            return
        self.close()

    def run(self):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.mainloop()
        return 0 if self.accepted else 2


def demo_catalog() -> Catalog:
    nodes = [Node("folder-root", -1, "Plans & Docs"),
             Node("folder-date", 0, "2026-01-01"),
             Node("folder-interior", 1, "Permit set")]
    names = ["A-001 | Cover sheet", "ID2.1 | 1st Floor", "ID2.2 | 2nd Floor", "ID2.3 | 3rd Floor",
             "ID3.1 | Enlarged plans", "ID4.1 | Amenity elevations", "ID4.2 | Restroom elevations",
             "ID5.1 | Finish details", "ID6.1 | Schedules", "ID7.1 | Millwork"]
    for i, name in enumerate(names):
        nodes.append(Node(f"page-{i}", 2, name, True, i in (1, 2, 4, 5), i != 0, i == 0))
    parent = len(nodes)
    nodes.append(Node("folder-architect", 0, "2026-08-28 Architectural Set"))
    for i, name in enumerate(("A-000 | General notes", "A-101 | Ground floor", "A-102 | Typical floor",
                              "A-201 | Elevations", "A-301 | Sections")):
        nodes.append(Node(f"arch-{i}", parent, name, True, i == 1, i in (1, 2)))
    return Catalog("demo", "Community sample project", "page-1", tuple(nodes))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?", type=Path)
    parser.add_argument("result", nargs="?", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args(argv)
    if not args.demo and (args.request is None or args.result is None):
        parser.error("request and result paths are required, or use --demo")
    try:
        if args.demo:
            return PickerWindow(demo_catalog()).run()
        # Each launch has a private request directory. A prior result must
        # never turn Cancel or an error into an export.
        args.result.unlink(missing_ok=True)
        catalog = read_catalog(args.request)
        return PickerWindow(catalog, args.result, state_path=args.state).run()
    except Exception as exc:
        if args.result is not None:
            try:
                args.result.with_suffix(".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
        try:
            from tkinter import messagebox
            messagebox.showerror("Could not open PDF export", str(exc))
        except Exception:
            pass
        if sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
