# SPDX-License-Identifier: AGPL-3.0-only
#!/usr/bin/env python3
"""
pdf_takeoff_builder.py — PDF Takeoff Export (Python side)

Reads the manifest written by the separately supplied PlanSwift script, embeds each page's
selected TIFF or source PDF background, draws takeoff as vector objects on PDF layers
(OCGs, one per takeoff item), adds a searchable per-page legend, and saves a
single merged deliverable PDF.

Usage:
    python pdf_takeoff_builder.py <manifest.txt>

Validation: legacy linear totals are compared with drawn lengths. Calculated
Qty totals are reported separately because formulas can change their units.
A report is printed; incomplete exports retain the manifest and report beside
the output PDF. Successful exports remove those diagnostic files.

Requires: pymupdf  (pip install pymupdf)
"""

import base64
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF is not installed. Run:  pip install pymupdf")

from pdf_source_paths import (clean_sheet_id, resolve_source_pdf, resolve_page_folder,
                              SourceResolver, validate_source_page)

DEFAULT_THICKNESS_IN = 1.0       # fallback wall/line thickness, in inches,
                                  # used when an item has no numeric thickness
DEFAULT_LINE_WIDTH_PT = 3.0       # last-resort flat width if scale is unknown
# Fixed takeoff stroke widths, in POINTS. Explicit non-zero widths so every PDF
# viewer renders the same weight. A to-scale thickness lands sub-1pt, which
# Revu/Foxit "thin-line enhance" into a thick line while PDF-XChange shows it
# true-thin — so the same file looks different per viewer. Fixed ~>=1pt widths
# render identically everywhere; width 0 (device hairline) would diverge too.
LINEAR_WIDTH_PT = 2.0             # linear / segment / wall-area lines (solid)
AREA_PERIMETER_PT = 0.75         # area outline — a thin line, not a scaled band
HATCH_LINE_WIDTH_PT = 0.5        # stroke width of hatch pattern lines
# .PAT deltas are in drawing units; x Hatch Pattern Scale = real size, then
# x points-per-foot puts it on the sheet. HATCH_SCALE_ADJUST is one knob to
# match PlanSwift's density if the base unit differs.
HATCH_SCALE_ADJUST = 1.0
STROKE_OPACITY = 0.75
AREA_FILL_OPACITY = 0.30
LAYER_PREFIX = "Takeoff | "      # groups takeoff layers visually in the panel
# Page point-size: each output page is built from the PlanSwift page TIFF. By
# default we trust the TIFF's own DPI tag for the paper size; set this to the
# PlanSwift raster DPI (e.g. 150) to force it if the TIFFs aren't DPI-tagged or
# the fixed line weights look off. Alignment is unaffected either way.
PAGE_DPI_OVERRIDE = None
LEGEND_FONT = "helv"
LEGEND_FONT_BOLD = "hebo"
LEGEND_FONT_SIZE = 13.0
LEGEND_ROW_H = 26.0
LEGEND_SWATCH = 18.0
# When a Count uses an Icon symbol, its legend row shows the icon (fitted,
# aspect preserved) in place of the color swatch. Cell size as multiples of the
# legend font — big enough to read the icon's shape, not its text (the unit name
# is printed next to it). Tune these if you want the icons larger/smaller.
LEGEND_SYMBOL_W_MULT = 3.6
LEGEND_SYMBOL_H_MULT = 2.2
LEGEND_X = 340.0                 # offset from displayed page top-left, points
LEGEND_Y = 40.0
COUNT_MARKER_RADIUS_PT = 6.0
MIN_FONT = 6.0                   # smallest readable legend text (fallback size)
AUTO_LEGEND_MAX_FONT = 24.0
AUTO_LEGEND_CLEARANCE = 12.0     # PDF points between legend and takeoff


def automatic_legend_layout(display, bounds, width_at, height_at):
    """Fit one-line rows strictly right of all tracing bounds.

    Bounds are in displayed PDF coordinates and include stroke/icon extents.
    Prefer half-point sizes down to MIN_FONT, then shrink below it as needed.
    Return None only when even the fixed padding cannot fit in the strip.
    """
    right, top = display.x1 - 20, display.y0 + 20
    minimum_left = max(display.x0 + 20,
                       bounds.x1 + AUTO_LEGEND_CLEARANCE
                       if bounds is not None else display.x0 + 20)
    for half_points in range(int(AUTO_LEGEND_MAX_FONT * 2),
                             int(MIN_FONT * 2) - 1, -1):
        fs = half_points / 2.0
        box = fitz.Rect(right - width_at(fs), top,
                        right, top + height_at(fs))
        if box.x0 < minimum_left or box.y1 > display.y1 - 10:
            continue
        return fs, box
    # Long descriptions must remain on one line. Below the old six-point
    # floor, solve for the largest fitting size instead of adding a page.
    available_w = right - minimum_left
    available_h = display.y1 - 10 - top
    if width_at(0) >= available_w or height_at(0) >= available_h:
        return None
    lo, hi = 0.0, MIN_FONT
    for _ in range(40):
        fs = (lo + hi) / 2
        if width_at(fs) <= available_w and height_at(fs) <= available_h:
            lo = fs
        else:
            hi = fs
    fs = lo * (1 - 1e-9)  # Stay inside the boundary despite floating-point error.
    return fs, fitz.Rect(right - width_at(fs), top, right, top + height_at(fs))


def adaptive_legend_layout(display, bounds, row_count, width_at, height_at):
    """Allow a wider top-right legend for up to five entries in one page half.

    This is a geometry rule, not sheet-type detection. Mixed-half and larger
    legends retain the strict right-hand strip, including small-font fitting.
    """
    standard = automatic_legend_layout(display, bounds, width_at, height_at)
    if bounds is None or not 1 <= row_count <= 5:
        return standard, "right-strip"
    midpoint = (display.y0 + display.y1) / 2
    fs = 12.0
    width, height = width_at(fs), height_at(fs)
    if not (bounds.y0 >= midpoint or bounds.y1 <= midpoint):
        return standard, "right-strip"
    if standard is not None and standard[0] >= fs:
        return standard, "right-strip"
    right, top = display.x1 - 20, display.y0 + 20
    box = fitz.Rect(right - width, top, right, top + height)
    mode = "five-or-fewer / wider top-right"
    protected = bounds + fitz.Rect(-AUTO_LEGEND_CLEARANCE, -AUTO_LEGEND_CLEARANCE,
                                   AUTO_LEGEND_CLEARANCE, AUTO_LEGEND_CLEARANCE)
    page_inset = display + fitz.Rect(20, 20, -20, -20)
    if page_inset.contains(box) and not box.intersects(protected):
        return (fs, box), mode
    return standard, "right-strip (12 pt corner did not fit)"

# Page-multiplier badge — stamped on every page that carries takeoff, ALWAYS
# (even at the default 1), so a reader of the PDF knows whether the sheet's
# takeoff is typical-floor multiplied. Red bold text on a pale highlight box,
# anchored at the displayed page's top-left corner.
MULT_X = 40.0                    # offset from displayed page top-left, points
MULT_Y = 40.0
MULT_FONT_FRAC = 0.012           # badge font as a fraction of displayed width
MULT_FONT_MIN = 10.0
MULT_TEXT_COLOR = (0.80, 0.0, 0.0)
MULT_BG_COLOR = (1.0, 1.0, 0.72)


# --------------------------------------------------------------------------
# Manifest parsing
# --------------------------------------------------------------------------

def parse_kv(line):
    m = re.match(r"^([A-Za-z#]+):\s?(.*)$", line)
    return (m.group(1), m.group(2)) if m else (None, None)


def parse_manifest(path):
    """Parse the line-based manifest into a dict tree."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip("\r\n") for ln in f]

    job = {"pages": []}
    page = item = section = subtract = dimension = highlighter = note = None
    in_digitizer = False
    digitizer_buf = []
    digitizer_target = None       # dict the current DigitizerData belongs to

    in_legend = False
    legend_buf = []
    in_hatch = False
    hatch_buf = []

    # A section's DigitizerData now ends at the first nested-block boundary (a
    # SUBTRACT block) as well as at SECTION_END.
    boundary = ("--- SECTION_END ---", "--- SUBTRACT_START ---",
                "--- SUBTRACT_END ---", "--- DIMENSION_START ---",
                "--- DIMENSION_END ---", "--- HIGHLIGHTER_START ---",
                "--- HIGHLIGHTER_END ---", "--- NOTE_START ---",
                "--- NOTE_END ---", "--- PAGE_END ---")
    for ln in lines:
        marker = ln.strip()
        if in_digitizer:
            if marker in boundary:
                if digitizer_target is not None:
                    digitizer_target["digitizer_xml"] = \
                        "\n".join(digitizer_buf).strip()
                in_digitizer = False
                digitizer_target = None
                # fall through so this boundary marker is handled below
            else:
                digitizer_buf.append(ln)
                continue
        if in_legend:
            if marker == "LegendDataEnd":
                if page is not None:
                    page["legend_xml"] = "\n".join(legend_buf).strip()
                in_legend = False
            else:
                legend_buf.append(ln)
            continue
        if in_hatch:
            if marker == "HatchDataEnd":
                if item is not None:
                    item["hatch_pat"] = "\n".join(hatch_buf).strip()
                in_hatch = False
            else:
                hatch_buf.append(ln)
            continue

        if marker == "--- PAGE_START ---":
            page = {"items": []}
            job["pages"].append(page)
        elif marker == "--- PAGE_END ---":
            page = None
        elif marker == "--- ITEM_START ---":
            item = {"sections": []}
            page["items"].append(item)
        elif marker == "--- ITEM_END ---":
            item = None
        elif marker == "--- SECTION_START ---":
            section = {}
            subtract = None
            item["sections"].append(section)
        elif marker == "--- SECTION_END ---":
            section = None
            subtract = None
        elif marker == "--- SUBTRACT_START ---":
            subtract = {}
            if section is not None:
                section.setdefault("subtracts", []).append(subtract)
        elif marker == "--- SUBTRACT_END ---":
            subtract = None
        elif marker == "--- DIMENSION_START ---":
            dimension = {}
            if page is not None:
                page.setdefault("dimensions", []).append(dimension)
        elif marker == "--- DIMENSION_END ---":
            dimension = None
        elif marker == "--- HIGHLIGHTER_START ---":
            highlighter = {}
            if page is not None:
                page.setdefault("highlighters", []).append(highlighter)
        elif marker == "--- HIGHLIGHTER_END ---":
            highlighter = None
        elif marker == "--- NOTE_START ---":
            note = {}
            if page is not None:
                page.setdefault("notes", []).append(note)
        elif marker == "--- NOTE_END ---":
            note = None
        elif marker.startswith("DigitizerData"):
            in_digitizer = True
            digitizer_buf = []
            digitizer_target = (note if note is not None else
                                (highlighter if highlighter is not None else
                                 (dimension if dimension is not None else
                                  (subtract if subtract is not None else section))))
        elif marker == "LegendData:":
            in_legend = True
            legend_buf = []
        elif marker == "HatchData:":
            in_hatch = True
            hatch_buf = []
        else:
            key, val = parse_kv(ln)
            if key is None:
                continue
            target = note if note is not None else (
                highlighter if highlighter is not None else (
                    dimension if dimension is not None else (
                        subtract if subtract is not None else (
                            section if section is not None else (
                                item if item is not None else (
                                    page if page is not None else job))))))
            target[key] = val
    from pdf_section_records import normalize_section_records
    return normalize_section_records(job)


def parse_points(xml_text):
    """DigitizerData XML -> [(x, y), ...] in TIFF pixel space."""
    if not xml_text or "<Points" not in xml_text:
        return []
    root = ET.fromstring(xml_text)
    return [(float(p.get("X")), float(p.get("Y"))) for p in root.iter("Point")]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def tcolor_to_rgb01(color_str):
    """PlanSwift/Delphi color -> (r,g,b) floats. Int format: R + G*256 + B*65536.
    Also accepts '$00BBGGRR' hex and 'clXxx' names (small common subset)."""
    named = {
        "clred": (1, 0, 0), "clgreen": (0, .5, 0), "clblue": (0, 0, 1),
        "clyellow": (1, 1, 0), "clblack": (0, 0, 0), "clwhite": (1, 1, 1),
        "clgray": (.5, .5, .5), "clmaroon": (.5, 0, 0), "clnavy": (0, 0, .5),
        "clolive": (.5, .5, 0), "clpurple": (.5, 0, .5), "clteal": (0, .5, .5),
        "cllime": (0, 1, 0), "claqua": (0, 1, 1), "clfuchsia": (1, 0, 1),
    }
    s = (color_str or "").strip()
    if s.lower() in named:
        return named[s.lower()]
    if s.startswith("$"):
        v = int(s[1:], 16)
    else:
        try:
            v = int(float(s))
        except ValueError:
            return (1, 0, 0)
    v &= 0xFFFFFF
    return ((v & 0xFF) / 255.0, ((v >> 8) & 0xFF) / 255.0,
            ((v >> 16) & 0xFF) / 255.0)



def pdf_item_color(item):
    """Community edition keeps the takeoff item's PlanSwift color."""
    return item.get('Color', '')


def ceil_qty_str(total_str):
    """Round a PlanSwift total UP to the nearest integer for legend display
    (e.g. 111.01 -> '112'). Validation/report values are unaffected — this is
    used only for what's printed in the legend box."""
    try:
        # Round away float noise (e.g. 100*0.1 -> 10.000000000000002) before
        # ceiling — otherwise a value that is really a whole number rounds UP by
        # one. 6 dp strips ~1e-14 noise while keeping genuine takeoff decimals.
        return str(math.ceil(round(float(total_str), 6)))
    except (TypeError, ValueError):
        return total_str


def polyline_len(pts, closed=False):
    n = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    if closed and len(pts) > 2:
        n += math.dist(pts[-1], pts[0])
    return n


def parse_pat(text):
    """Parse an AutoCAD .PAT hatch definition into line families.
    Each family = (angle_deg, x0, y0, delta_x, delta_y, [dash1, dash2, ...]).
    The '*name' header, blanks and ';' comments are skipped."""
    fams = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("*") or ln.startswith(";"):
            continue
        try:
            nums = [float(p.strip()) for p in ln.split(",") if p.strip() != ""]
        except ValueError:
            continue
        if len(nums) < 5:
            continue
        fams.append((nums[0], nums[1], nums[2], nums[3], nums[4], nums[5:]))
    return fams


def _dash_runs(xa, xb, pat, anchor):
    """Pen-down sub-intervals of [xa, xb] for a signed dash pattern 'pat'
    (points; + = pen down, - = pen up; no zero elements). The dash cycle is
    ANCHORED at x = anchor — .PAT dashes are positioned by the pattern's
    origin, not by wherever a clipped span happens to start."""
    lengths = [abs(v) for v in pat]
    ison = [v > 0 for v in pat]
    cycle = sum(lengths)
    if cycle <= 1e-9:
        yield (xa, xb)
        return
    p = (xa - anchor) % cycle
    j = 0
    while p >= lengths[j]:
        p -= lengths[j]
        j = (j + 1) % len(lengths)
    rem = lengths[j] - p
    x = xa
    guard = 0
    while x < xb - 1e-9 and guard < 500000:
        guard += 1
        step = min(rem, xb - x)
        if ison[j] and step > 1e-9:
            yield (x, x + step)
        x += step
        rem -= step
        if rem <= 1e-9:
            j = (j + 1) % len(lengths)
            rem = lengths[j]


def draw_hatch(pdf_page, rings, fams, unit_pts, rgb, oc, derot):
    """Fill a polygon with a .PAT hatch, clipped to its boundary and holes.

    rings   : [exterior, hole1, ...], (x, y) lists in the page's DISPLAY space.
    fams    : parsed .PAT families (angle, x0, y0, dx, dy, dashes).
    unit_pts: points per .PAT unit (Hatch Pattern Scale x points/ft x adjust).

    Exact AutoCAD .PAT semantics, computed in y-UP pattern space (display y is
    flipped in and back out, so 45 deg slopes the same way as in PlanSwift):
    rotate the plane by -angle so the family's lines run horizontal; with
    O' = R(-angle) * (x0, y0) * U, line n sits at y' = O'y + n*dy*U and its
    dash cycle is anchored at x' = O'x + n*dx*U. Origin anchoring is what
    registers the families to each other (e.g. the 7 carpet families assemble
    into arrows); per-line dx shifts the anchor (brick/carpet stagger).
    Scanline clipping drops the holes via even-odd pairing of crossings.
    """
    if not rings or not fams or unit_pts <= 0:
        return
    rings_up = [[(p[0], -p[1]) for p in ring] for ring in rings]
    for angle, x0, y0, dx, dy, dashes in fams:
        spacing = abs(dy) * unit_pts
        if spacing < 0.5:            # guard against absurd density
            spacing = 0.5
        th = math.radians(angle)
        ca, sa = math.cos(-th), math.sin(-th)      # rotate by -angle
        ica, isa = math.cos(th), math.sin(th)      # rotate back
        ox = (x0 * unit_pts) * ca - (y0 * unit_pts) * sa
        oy = (x0 * unit_pts) * sa + (y0 * unit_pts) * ca

        edges = []
        ys = []
        for ring in rings_up:
            rp = [(px * ca - py * sa, px * sa + py * ca) for px, py in ring]
            m = len(rp)
            for k in range(m):
                a = rp[k]
                b = rp[(k + 1) % m]
                if a[1] != b[1]:
                    edges.append((a, b))
                ys.append(a[1])
        if not edges:
            continue
        ymin, ymax = min(ys), max(ys)

        raw = [d * unit_pts for d in dashes if abs(d) > 1e-9] if dashes else []
        dpat = raw if (any(v > 0 for v in raw) and any(v < 0 for v in raw)) else None
        dx_pts = dx * unit_pts

        sh = pdf_page.new_shape()
        drew = False
        n0 = math.ceil((ymin - oy) / spacing - 1e-9)
        n1 = math.floor((ymax - oy) / spacing + 1e-9)
        for n in range(n0, n1 + 1):
            y = oy + n * spacing
            anchor = ox + n * dx_pts
            xs = []
            for a, b in edges:
                y1, y2 = a[1], b[1]
                if (y1 <= y < y2) or (y2 <= y < y1):
                    t = (y - y1) / (y2 - y1)
                    xs.append(a[0] + t * (b[0] - a[0]))
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                xa, xb = xs[k], xs[k + 1]
                runs = (_dash_runs(xa, xb, dpat, anchor) if dpat
                        else [(xa, xb)])
                for ra, rb in runs:
                    ux1, uy1 = ra * ica - y * isa, ra * isa + y * ica
                    ux2, uy2 = rb * ica - y * isa, rb * isa + y * ica
                    p1 = fitz.Point(ux1, -uy1) * derot      # flip back to display
                    p2 = fitz.Point(ux2, -uy2) * derot
                    sh.draw_line(p1, p2)
                    drew = True
        if drew:
            sh.finish(color=rgb, width=HATCH_LINE_WIDTH_PT, lineCap=1, oc=oc)
            sh.commit()


def append_source_pdf(out, path, page_data, greyscale, source_page=None, expected_label=None):
    """Copy a single source sheet without rasterizing, using its displayed size.

    TIFF pixels remain the coordinate reference for the overlaid takeoffs.
    Reject incompatible orientations/aspects rather than stretch the drawing.
    Source documents are changed only in memory, never saved over the originals.
    """
    with fitz.open(path) as source:
        sheet, rect, source_page = validate_source_page(source, source_page, expected_label, page_data)
        # Import the unrotated crop with an explicit rotation. Baking rotation
        # into the source page can shift a nonzero crop origin. show_pdf_page's
        # rotation is counterclockwise, opposite to the PDF /Rotate value.
        rotation = sheet.rotation
        sheet.set_rotation(0)
        if greyscale and page_data.get("items"):
            sheet.recolor(components=1)
        target = out.new_page(width=rect.width, height=rect.height)
        try:
            # Register overlay fonts at page scope before importing nested PDF
            # resources. Otherwise a source's /helv can be mistaken for a page
            # font and legend descriptions disappear in stricter PDF readers.
            target.insert_font(fontname=LEGEND_FONT)
            target.insert_font(fontname=LEGEND_FONT_BOLD)
            target.show_pdf_page(target.rect, source, source_page, rotate=-rotation)
        except Exception:
            out.delete_page(len(out) - 1)
            raise


def multiplier_badge_position(display, width, height, obstacles):
    """Prefer the four corners, then clear positions along the page edges.

    Obstacles are individual section/marker rectangles plus this page's legend
    in displayed coordinates. Keep six points clear of their visible extents.
    """
    if width > display.width or height > display.height:
        return None
    mx = min(MULT_X, (display.width - width) / 2)
    my = min(MULT_Y, (display.height - height) / 2)
    left, right = display.x0 + mx, display.x1 - mx - width
    top, bottom = display.y0 + my, display.y1 - my - height
    blocked = [r + fitz.Rect(-6, -6, 6, 6) for r in obstacles]

    def clear(x, y):
        rect = fitz.Rect(x, y, x + width, y + height)
        if display.contains(rect) and not any(rect.intersects(r) for r in blocked):
            return rect
        return None

    for x, y in ((left, top), (right, top), (right, bottom), (left, bottom)):
        rect = clear(x, y)
        if rect is not None:
            return rect
    # If all four corners are occupied, look just beyond obstacle edges.
    for r in blocked:
        for x, y in ((r.x1, top), (r.x0 - width, top),
                     (r.x1, bottom), (r.x0 - width, bottom),
                     (left, r.y1), (left, r.y0 - height),
                     (right, r.y1), (right, r.y0 - height)):
            if not (left <= x <= right and top <= y <= bottom):
                continue
            rect = clear(x, y)
            if rect is not None:
                return rect
    return None


def add_layer_action(page, hit_rect, derot, label, commands):
    """Add a persistent native PDF link with ordered ON/OFF/Toggle operations."""
    doc = page.parent
    state = []
    for operation, groups in commands:
        if groups:
            state.append("/" + operation)
            state.extend("%d 0 R" % group for group in dict.fromkeys(groups))
    pdf_rect = hit_rect * derot * ~page.transformation_matrix
    link = doc.get_new_xref()
    doc.update_object(link,
        "<< /Type /Annot /Subtype /Link /Rect [%s] /Border [0 0 0] /H /I "
        "/Contents %s /A << /S /SetOCGState /State [%s] /PreserveRB true >> >>" %
        (" ".join("%.6f" % value for value in pdf_rect),
         fitz.get_pdf_str(label), " ".join(state)))
    kind, annots = doc.xref_get_key(page.xref, "Annots")
    if kind == "xref":
        annots = doc.xref_object(int(annots.split()[0]), compressed=True)
    elif kind == "null":
        annots = "[]"
    doc.xref_set_key(page.xref, "Annots", annots.rstrip()[:-1] + " %d 0 R]" % link)
    return link


def configure_selection_layers(doc, selection_groups):
    """Keep selection layers unlisted and exclude them from print/export events."""
    if not selection_groups:
        return
    catalog = doc.pdf_catalog()
    # add_ocg appends all new groups to /Order. Remove only our helper OCGs,
    # retaining the original takeoff and drawing layers in their existing order.
    kind, order = doc.xref_get_key(catalog, 'OCProperties/D/Order')
    if kind == 'xref':
        order = doc.xref_object(int(order.split()[0]), compressed=True)
    if kind != 'null':
        hidden = set(selection_groups)
        order = re.sub(r'\b(\d+)\s+0\s+R\b',
                       lambda m: '' if int(m[1]) in hidden else m[0], order)
        doc.xref_set_key(catalog, 'OCProperties/D/Order', order)
    refs = ' '.join('%d 0 R' % group for group in selection_groups)
    for group in selection_groups:
        doc.xref_set_key(group, 'Usage/Print', '<< /PrintState /OFF >>')
        doc.xref_set_key(group, 'Usage/Export', '<< /ExportState /OFF >>')
    kind, settings = doc.xref_get_key(catalog, 'OCProperties/D/AS')
    if kind == 'xref':
        settings = doc.xref_object(int(settings.split()[0]), compressed=True)
    elif kind == 'null':
        settings = '[]'
    events = ' '.join('<< /Event /%s /Category [/%s] /OCGs [%s] >>' % (event, event, refs)
                      for event in ('Print', 'Export'))
    doc.xref_set_key(catalog, 'OCProperties/D/AS', settings.rstrip()[:-1] + events + ']')


def draw_selection_overlay(page, paths, count_boxes, oc):
    """Bright paths and square vertex handles, on an initially hidden OCG."""
    before = set(page.get_contents())
    points = {}
    for vertices, closed in paths:
        for point in vertices:
            points[(point.x, point.y)] = point
    # White halo ensures the cyan selection stays visible on any source drawing.
    for color, width in (((1, 1, 1), 5), ((0, .65, .95), 2)):
        outline = page.new_shape()
        for vertices, closed in paths:
            if len(vertices) > 1:
                outline.draw_polyline(vertices + vertices[:1] if closed else vertices)
        for rect in count_boxes:
            outline.draw_rect(rect + fitz.Rect(-3, -3, 3, 3))
        outline.finish(color=color, width=width, lineCap=1, lineJoin=1,
                       closePath=False, oc=oc)
        outline.commit()
    handles = page.new_shape()
    for point in points.values():
        handles.draw_rect(fitz.Rect(point.x - 3, point.y - 3, point.x + 3, point.y + 3))
    if points:
        handles.finish(color=(0, 0, 0), fill=(1, 1, 1), width=.7, oc=oc)
        handles.commit()
    return [xref for xref in page.get_contents() if xref not in before]


def gate_selection_streams(doc, records):
    """Nest selection content inside its base OCG for consistent AND visibility.

    Some readers mishandle multi-group OCMD policies. Nested marked content
    makes both the takeoff and its selection layer necessary for visibility.
    """
    properties = {page.xref: {xref: name for name, xref, kind in page.get_oc_items()}
                  for page in doc}
    for page_xref, base, streams in records:
        name = properties[page_xref][base]
        for stream in streams:
            data = doc.xref_stream(stream)
            doc.update_stream(stream, ('/OC /%s BDC\n' % name).encode('ascii') + data + b'\nEMC\n')


def draw_legend_layer_toggle(page, rect, hit_rect, derot, oc, label, selections=()):
    """Persistent eye/link with a layer-dependent pupil and hidden-state slash.

    The link and outline are outside the takeoff OCG, so a hidden layer can
    always be restored. SetOCGState toggles the shared item on every page.
    """
    doc = page.parent
    x0, y0, x1, y1 = rect
    w, h = rect.width, rect.height
    cy = (y0 + y1) / 2
    left = fitz.Point(x0 + w * .06, cy) * derot
    right = fitz.Point(x1 - w * .06, cy) * derot
    eye = page.new_shape()
    eye.draw_bezier(left, fitz.Point(x0 + w * .3, y0 + h * .05) * derot,
                    fitz.Point(x1 - w * .3, y0 + h * .05) * derot, right)
    eye.draw_bezier(right, fitz.Point(x1 - w * .3, y1 - h * .05) * derot,
                    fitz.Point(x0 + w * .3, y1 - h * .05) * derot, left)
    eye.finish(color=(.25, .25, .25), width=w * .06, closePath=True)
    eye.commit()
    pupil = page.new_shape()
    pupil.draw_circle(fitz.Point((x0 + x1) / 2, cy) * derot, w * .14)
    pupil.finish(color=None, fill=(.15, .15, .15), oc=oc)
    pupil.commit()
    off = doc.set_ocmd(ocgs=[oc], policy="AllOff")
    slash = page.new_shape()
    slash.draw_line(fitz.Point(x0 + w * .12, y1 - h * .12) * derot,
                    fitz.Point(x1 - w * .12, y0 + h * .12) * derot)
    slash.finish(color=(.45, .45, .45), width=w * .09, oc=off)
    slash.commit()

    return add_layer_action(page, hit_rect, derot, "Show/hide takeoff: " + label,
                            [('OFF', selections), ('Toggle', [oc])])


def draw_multiplier_badge(pdf_page, disp, derot, mult_raw, obstacles=()):
    """Stamp 'PAGE MULTIPLIER: N' in a clear corner (or clear edge space) —
    red bold text on a pale highlight box. Drawn on every takeoff page with no
    OCG, so it is always visible and cannot be toggled off: quantities on the
    sheet are multiplied by this factor, and a reader must see it."""
    raw = str(mult_raw if mult_raw is not None else "").replace(",", "").strip()
    try:
        label = "%g" % float(raw) if raw else "1"   # 1.00 -> 1, 2.50 -> 2.5
    except ValueError:
        label = raw                                  # show whatever was reported
    txt = "PAGE MULTIPLIER: %s" % label
    fs = max(MULT_FONT_MIN, disp.width * MULT_FONT_FRAC)
    while True:
        tw = fitz.get_text_length(txt, fontname=LEGEND_FONT_BOLD, fontsize=fs)
        pad = fs * 0.45
        box_h = fs * 1.6
        box = multiplier_badge_position(disp, tw + 2 * pad, box_h, obstacles)
        if box is not None:
            break
        if fs <= MULT_FONT_MIN:
            raise ValueError("no clear corner or edge space for page multiplier; "
                             "badge not drawn to avoid covering legend or takeoff")
        fs = max(MULT_FONT_MIN, fs - 1)
    x0, y0 = box.x0, box.y0
    sh = pdf_page.new_shape()
    sh.draw_rect(box * derot)
    sh.finish(color=MULT_TEXT_COLOR, fill=MULT_BG_COLOR,
              width=max(0.8, fs * 0.06))
    sh.commit()
    base = fitz.Point(x0 + pad, y0 + box_h / 2.0 + fs * 0.35) * derot
    pdf_page.insert_text(base, txt, fontname=LEGEND_FONT_BOLD, fontsize=fs,
                         color=MULT_TEXT_COLOR, rotate=pdf_page.rotation)
    return box


def tiff_pixel_size(path):
    """Pixel (width, height) of an image file, or None if unreadable.

    The page TIFF's own size is the authoritative page size — digitizer
    coordinates live in this image's pixel space. Reads as little as possible:
    fitz.image_profile on the first 64 KB (enough when the IFD sits at the
    front), then on the whole file (TIFF writers often park the IFD at the
    end), then a full decode as the last resort."""
    def _ok(prof):
        return prof and prof.get("width", 0) > 0 and prof.get("height", 0) > 0
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
        prof = None
        try:
            prof = fitz.image_profile(head)
        except Exception:
            prof = None
        if not _ok(prof):
            with open(path, "rb") as f:
                data = f.read()
            try:
                prof = fitz.image_profile(data)
            except Exception:
                prof = None
        if _ok(prof):
            return int(prof["width"]), int(prof["height"])
        pix = fitz.Pixmap(path)
        w, h = int(pix.width), int(pix.height)
        pix = None
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def resolve_page_tiff(page_folder):
    """Locate a page's rasterized TIFF from its PlanSwift storage folder.

    The Page's 'Folder Path' property gives its on-disk folder, which holds a
    'Data.xml' (PlanSwift's property store) plus the page image named by a GUID.
    That image GUID is NOT exposed through the PlanSwift API, so we read it from
    Data.xml: the <Property> with Name='Image' / Class='Large Image' /
    ImageType='TTiffGraphic' carries GUID='{...}', and the file is '{GUID}.tiff'
    in the same folder. Returns (tiff_path, err).
    """
    if not page_folder or not os.path.isdir(page_folder):
        return None, "page folder missing: %r" % page_folder
    data_xml = os.path.join(page_folder, "Data.xml")
    if not os.path.isfile(data_xml):
        return None, "no Data.xml in %r" % page_folder
    try:
        with open(data_xml, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except OSError as e:
        return None, "cannot read Data.xml: %s" % e

    def guid_of(tag):
        gm = re.search(r'\bGUID="([^"]+)"', tag)
        return gm.group(1) if gm else None

    guid = None
    tags = re.findall(r"<Property\b[^>]*?/?>", xml)
    for tag in tags:                                   # the page image
        if 'Name="Image"' in tag and 'ImageType="TTiffGraphic"' in tag:
            guid = guid_of(tag)
            if guid:
                break
    if not guid:                                       # looser fallback
        for tag in tags:
            if 'ImageType="TTiffGraphic"' in tag or 'Class="Large Image"' in tag:
                guid = guid_of(tag)
                if guid:
                    break
    if not guid:
        return None, "no page-image (TTiffGraphic) property in Data.xml"
    exact = os.path.join(page_folder, guid + ".tiff")
    if os.path.isfile(exact):
        return exact, None
    hits = [f for f in os.listdir(page_folder)         # .tif / case fallback
            if os.path.splitext(f)[0].lower() == guid.lower()]
    if hits:
        return os.path.join(page_folder, hits[0]), None
    return None, "image file %s.tiff not found in %r" % (guid, page_folder)


def _norm_guid(g):
    return (g or "").replace("{", "").replace("}", "").replace(" ", "").upper()


def _read_item_guid(data_xml_path):
    """Root <Item ... GUID="..."> from a Data.xml head. Returns GUID or None."""
    try:
        with open(data_xml_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return None
    m = re.search(r'<Item\b[^>]*?\bGUID="([^"]*)"', head)
    return m.group(1) if m else None


def _find_child_folder(parent_folder, target_guid):
    """Immediate subfolder of parent_folder whose Data.xml root GUID matches
    (bracket-proof descent — folder NAMES are ignored)."""
    tgt = _norm_guid(target_guid)
    try:
        entries = os.listdir(parent_folder)
    except OSError:
        return None
    for name in entries:
        sub = os.path.join(parent_folder, name)
        dx = os.path.join(sub, "Data.xml")
        if os.path.isdir(sub) and os.path.isfile(dx):
            g = _read_item_guid(dx)
            if g and _norm_guid(g) == tgt:
                return sub
    return None


def _find_image_data(xml_text, prop_name):
    """base64 ImageData for the <Property Name="prop_name" ImageData="..."> tag."""
    pn = re.escape(prop_name)
    m = re.search(r'Name="%s"[^>]*?\bImageData="([^"]*)"' % pn, xml_text)
    if m:
        return m.group(1)
    m = re.search(r'\bImageData="([^"]*)"[^>]*?Name="%s"' % pn, xml_text)
    return m.group(1) if m else None


def resolve_symbol_icon(anchor_folder, guid_chain):
    """Descend the GUID chain from anchor_folder to the count item's folder
    (matching each subfolder's Data.xml root <Item GUID> — the only path method
    that survives bracketed template-folder names), then pull its 'Symbol Icon'
    image (base64 in ImageData). Returns (raw_image_bytes, err)."""
    folder = anchor_folder
    if not folder or not os.path.isdir(folder):
        return None, "anchor folder missing: %r" % anchor_folder
    for g in guid_chain:
        nxt = _find_child_folder(folder, g)
        if nxt is None:
            return None, "no subfolder GUID matches %s under %s" % (g, folder)
        folder = nxt
    dx = os.path.join(folder, "Data.xml")
    if not os.path.isfile(dx):
        return None, "Data.xml not in item folder: %s" % folder
    try:
        with open(dx, "r", encoding="utf-8", errors="replace") as f:
            xml_text = f.read()
    except OSError as e:
        return None, "cannot read item Data.xml: %s" % e
    b64 = _find_image_data(xml_text, "Symbol Icon")
    if not b64:
        return None, "no 'Symbol Icon' ImageData in %s" % dx
    try:
        return base64.b64decode(b64), None
    except Exception as e:
        return None, "Symbol Icon base64 decode failed: %s" % e


def icon_pixmap(raw):
    """Decode icon bytes into a fitz.Pixmap, keying magenta (255,0,255) to
    transparent as a safety net (PlanSwift's Symbol Icons are PNGs that already
    carry alpha, so this is a no-op for them). PyMuPDF only — no numpy/PIL."""
    try:
        pm = fitz.Pixmap(raw)
    except Exception:
        return None
    try:
        if pm.colorspace is None:
            return None
        if (pm.n - pm.alpha) > 3:                 # CMYK -> RGB
            pm = fitz.Pixmap(fitz.csRGB, pm)
        if not pm.alpha:
            pm = fitz.Pixmap(pm, 1)               # ensure an alpha channel
        data = bytearray(pm.samples)
        n = pm.n
        keyed = False
        for i in range(0, len(data), n):
            if data[i] == 255 and data[i + 1] == 0 and data[i + 2] == 255:
                data[i + 3] = 0
                keyed = True
        if keyed:
            pm = fitz.Pixmap(fitz.csRGB, pm.width, pm.height, bytes(data), True)
    except Exception:
        pass
    return pm


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def warn_missing_sources(skipped):
    """Warn the user about pages that were dropped (page folder/TIFF missing, or
    unreadable page size) so a run that otherwise 'succeeds' can't silently ship
    a set with pages missing. Uses a native Windows dialog; if that's not
    available (non-Windows, or it errors) it falls back to a console pause so the
    list is still seen. No-op when nothing was skipped."""
    if not skipped:
        return
    rows = "\n".join("    %s  —  %s" % (name, reason) for name, reason in skipped)
    body = ("%d page(s) were skipped and are NOT in the exported PDF:\n\n%s\n\n"
            "Check that each page's storage folder (its 'Folder Path' property) "
            "exists and holds the page image (Data.xml + .tiff). For Source PDF "
            "mode, also check the PDF folder, matching sheet filenames, and the "
            "source PDF page sizes/orientations. If PlanSwift "
            "shows the page's Image property as an error, restart PlanSwift, "
            "open the page once, and re-run the export." % (len(skipped), rows))
    try:
        import ctypes
        # MB_OK | MB_ICONWARNING | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(
            0, body, "Takeoff PDF Export — Pages Skipped", 0x40030)
        return
    except Exception:
        pass
    # No GUI available (or it failed) — keep the console open so it's readable.
    print("\n*** WARNING: PAGES SKIPPED ***\n" + body)
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass


GREY_JPEG_QUALITY = 80


def _recompress_page_rasters(doc, page, quality=GREY_JPEG_QUALITY, min_stream=3000):
    """Recompress a page's Flate raster images back to JPEG (DCTDecode) in their
    OWN colorspace — gray sheets -> gray JPEG, colour sheets -> colour JPEG —
    keeping each image's soft mask. Greyscaled takeoff sheets get stored as
    lossless Flate (bloating photo-heavy sheets; a cover render can 3x); this
    shrinks them without recolouring colour "Print Page"-only pages. Returns how
    many images were recompressed."""
    n = 0
    for img in page.get_images(full=True):
        xref = img[0]
        filt = doc.xref_get_key(xref, "Filter")[1] or ""
        if "DCTDecode" in filt:            # already JPEG
            continue
        raw = doc.xref_stream_raw(xref)
        if not raw or len(raw) < min_stream:
            continue
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.alpha:                  # drop embedded alpha; /SMask key stays
                pix = fitz.Pixmap(pix, 0)
            if pix.colorspace is None:
                continue
            # Recompress each raster in ITS OWN colorspace: the greyscaled
            # takeoff sheets are already gray -> gray JPEG; a colour page pulled
            # in only by "Print Page" stays colour. Do NOT force gray here — that
            # was greyscaling colour print-only pages that must stay in colour.
            is_gray = (pix.colorspace.n == 1)
            jpg = pix.tobytes("jpg", jpg_quality=quality)
            if len(jpg) >= len(raw):       # no gain -> leave as-is
                continue
            doc.update_stream(xref, jpg, compress=False)
            doc.xref_set_key(xref, "Filter", "/DCTDecode")
            doc.xref_set_key(xref, "ColorSpace",
                             "/DeviceGray" if is_gray else "/DeviceRGB")
            doc.xref_set_key(xref, "BitsPerComponent", "8")
            doc.xref_set_key(xref, "DecodeParms", "null")
            n += 1
        except Exception:
            continue
    return n


def shrink_greyscaled_rasters(path):
    """Reopen a saved export and recompress its greyscaled rasters to JPEG.
    recolor() only materializes the JPEG->Flate conversion at save time, so this
    runs on the already-written file, then rewrites it (garbage-collected) so the
    old Flate streams are dropped. No-op if nothing benefits."""
    try:
        d = fitz.open(path)
    except Exception:
        return
    n = 0
    for pg in d:
        n += _recompress_page_rasters(d, pg)
    if n:
        tmp = path + ".shrink.tmp"
        try:
            d.save(tmp, garbage=4, deflate=True)
            d.close()
            os.replace(tmp, path)
            return
        except Exception:
            pass
    d.close()


def _unique_icon(base_png, uid):
    """Return the legend icon PNG made unique for this placement by flipping the
    red low-bit of the first opaque pixels to encode `uid`. Legend type-icons are
    byte-identical across same-type rows, so PDF image dedup (save garbage=4)
    merges them into ONE object that can hold only one /OC layer — which makes
    hiding one item's layer hide that icon on every page. A genuine <=1 change to
    opaque bytes stays distinct through garbage collection yet is imperceptible
    (transparent pixels are skipped because encoders normalize their RGB away)."""
    try:
        pm = fitz.Pixmap(base_png)
        alpha = pm.alpha
        picked = []
        for y in range(pm.height):
            for x in range(pm.width):
                if (not alpha) or pm.pixel(x, y)[-1] >= 250:
                    picked.append((x, y))
                    if len(picked) >= 20:
                        break
            if len(picked) >= 20:
                break
        for b, (x, y) in enumerate(picked):
            px = list(pm.pixel(x, y))
            px[0] = (px[0] & ~1) | ((uid >> b) & 1)
            pm.set_pixel(x, y, tuple(px))
        return pm.tobytes("png")
    except Exception:
        return base_png


DIM_LINE_WIDTH_PT = 1.2          # dimension line + witness lines
DIM_ARROW_LEN_F = 0.7            # arrowhead length as a fraction of text height
DIM_ARROW_HW_F = 0.30            # arrowhead half-width  "        "
DIM_WITNESS_F = 0.5             # witness half-length     "        "
DIM_ARROW_MIN_F = 10.0          # draw arrowheads only if the dimension line is at
                                # least this many text-heights long (PlanSwift omits
                                # arrows on short dimensions so they don't overlap).
                                # Calibrated to PlanSwift: at text height 1', a
                                # 10'-0" dimension gets arrows and a 9'-4" one does
                                # not, i.e. the cutoff is 10 x the text height.
TEXT_HEIGHT_ADJUST = 1.0         # tune dimension label size vs PlanSwift
NOTE_BORDER_PT = 1.0             # note box border width (points); tunable
NOTE_TEXT_ADJUST = 1.0           # tune note text size (Auto Size OFF path only)
NOTE_HPAD_RATIO = 0.04           # horiz padding as fraction of box width (fit-to-box)
NOTE_VPAD_RATIO = 0.10           # vert padding as fraction of box height (fit-to-box)
NOTE_LINE_H = 1.20               # line height as a multiple of the font size


def format_feet_inches(length_ft, precision=16, fractions=True, inches=True,
                       dashes=True):
    """Format a length in feet as feet-inches like PlanSwift (e.g. 9'-2 1/4").
    precision = fraction denominator (16 -> nearest 1/16"). fractions/inches/
    dashes toggle the fractional part, the inches part, and the dash separator."""
    from math import gcd
    denom = int(round(precision)) if precision and precision >= 1 else 16
    if denom < 1:
        denom = 16
    total_in = length_ft * 12.0
    units = round(total_in * denom)
    total_in = units / denom
    feet = int(total_in // 12)
    rem = total_in - feet * 12
    whole = int(rem)
    frac = int(round((rem - whole) * denom))
    if frac == denom:
        whole += 1
        frac = 0
        if whole == 12:
            feet += 1
            whole = 0
    sep = "-" if dashes else " "
    if not inches:
        return "%d'" % feet
    s = "%d'%s%d" % (feet, sep, whole)
    if fractions and frac > 0:
        g = gcd(frac, denom)
        s += " %d/%d" % (frac // g, denom // g)
    return s + '"'


def draw_dimension(page, p1, p2, derot, line_rgb, text_rgb, label, text_pt,
                   arrowheads, oc):
    """Draw a PlanSwift-style dimension. p1/p2 are in the page's DISPLAY space
    (visually upright); everything is laid out there — so the label reads
    left-to-right in the user's view and its side is correct — then transformed
    to content space with `derot` (sheets are often rotated 90 deg).

    Line p1->p2 with perpendicular witness lines at the ends and outward
    filled-triangle arrowheads (line_rgb), and a label (text_rgb) aligned to the
    line. The label sits on the (dy,-dx) side of the RAW p1->p2 direction, so a
    horizontally-flipped dimension (reversed point order) puts the label under
    the line, as PlanSwift does. All on the shared Dimensions OCG (oc)."""
    p1 = fitz.Point(*p1)
    p2 = fitz.Point(*p2)
    d = p2 - p1
    L = abs(d)
    if L < 1e-6 or text_pt <= 0:
        return
    ux, uy = d.x / L, d.y / L
    nx, ny = -uy, ux                          # witness perpendicular (symmetric)
    alen = DIM_ARROW_LEN_F * text_pt
    ahw = DIM_ARROW_HW_F * text_pt
    wit = DIM_WITNESS_F * text_pt

    def C(x, y):                              # display -> content
        return fitz.Point(x, y) * derot

    sh = page.new_shape()
    sh.draw_line(C(p1.x, p1.y), C(p2.x, p2.y))
    sh.draw_line(C(p1.x + nx * wit, p1.y + ny * wit),
                 C(p1.x - nx * wit, p1.y - ny * wit))
    sh.draw_line(C(p2.x + nx * wit, p2.y + ny * wit),
                 C(p2.x - nx * wit, p2.y - ny * wit))
    sh.finish(color=line_rgb, width=DIM_LINE_WIDTH_PT, oc=oc)
    # Arrowheads only when the line is long enough (else PlanSwift omits them so
    # the two heads don't overlap); the line and witness lines still draw.
    if arrowheads and L >= DIM_ARROW_MIN_F * text_pt:
        for tip, dx, dy in [(p1, ux, uy), (p2, -ux, -uy)]:
            bx, by = tip.x + dx * alen, tip.y + dy * alen
            ah = page.new_shape()
            ah.draw_polyline([C(tip.x, tip.y), C(bx + nx * ahw, by + ny * ahw),
                              C(bx - nx * ahw, by - ny * ahw), C(tip.x, tip.y)])
            ah.finish(color=line_rgb, fill=line_rgb, width=0.3, oc=oc)
            ah.commit()
    sh.commit()

    # readable direction in DISPLAY (flip if pointing left so text reads L->R)
    if ux > 0 or (abs(ux) < 1e-9 and uy < 0):
        rx, ry = ux, uy
    else:
        rx, ry = -ux, -uy
    sx, sy = uy, -ux                          # label side from the RAW direction
    text_up = (ry, -rx)                       # ascender direction of the label
    cap = 0.72 * text_pt
    gap = 0.30 * text_pt
    off = gap if (sx * text_up[0] + sy * text_up[1]) > 0 else gap + cap
    mid = fitz.Point((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)
    tw = fitz.get_text_length(label, fontname="helv", fontsize=text_pt)
    base_disp = fitz.Point(mid.x - rx * (tw / 2) + sx * off,
                           mid.y - ry * (tw / 2) + sy * off)
    base_content = base_disp * derot
    # transform the readable direction to content (direction only, no shift), then
    # build the morph. PyMuPDF's morph flips the y-sign, so the matrix that makes
    # the content-space writing direction == rdir is (a,-b,b,a).
    origin = fitz.Point(0, 0) * derot
    rdc = fitz.Point(rx, ry) * derot - origin
    rl = abs(rdc) or 1.0
    rcx, rcy = rdc.x / rl, rdc.y / rl
    M = fitz.Matrix(rcx, -rcy, rcy, rcx, 0, 0)
    w = fitz.TextWriter(page.rect, color=text_rgb)
    w.append(base_content, label, fontsize=text_pt, font=fitz.Font("helv"))
    w.write_text(page, morph=(base_content, M), oc=oc)


def draw_highlighter(page, pts_disp, derot, rgb, alpha, is_linear,
                     line_width_pt, oc):
    """Draw a PlanSwift highlighter on the shared Highlighters OCG. pts_disp are
    the digitizer points in DISPLAY space; each is transformed to content with
    derot. Box mode (is_linear=False) is a filled semi-transparent polygon with
    sharp corners; Linear mode is a thick round-capped semi-transparent stroke of
    line_width_pt thickness. Transparency is the opacity (alpha)."""
    if len(pts_disp) < 2:
        return
    sh = page.new_shape()
    if is_linear:
        cpts = [fitz.Point(x, y) * derot for (x, y) in pts_disp]
        sh.draw_polyline(cpts)
        sh.finish(color=rgb, width=max(line_width_pt, 0.1), lineCap=1,
                  lineJoin=1, stroke_opacity=alpha, oc=oc)
    else:
        # Box: filled polygon from the corner points (handles rotated boxes). If
        # only 2 points came through, treat them as opposite corners of an
        # axis-aligned box in display space.
        if len(pts_disp) == 2:
            (x0, y0), (x1, y1) = pts_disp
            disp = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        else:
            disp = pts_disp
        cpts = [fitz.Point(x, y) * derot for (x, y) in disp]
        sh.draw_polyline(cpts + [cpts[0]])
        sh.finish(color=None, fill=rgb, fill_opacity=alpha, oc=oc)
    sh.commit()


def fit_note_text_pt(lines, box_w, box_h):
    """Largest font size (pt) at which the given text lines fit inside a box of
    box_w x box_h (display pt), leaving NOTE_HPAD_RATIO / NOTE_VPAD_RATIO margins.
    PlanSwift's Auto Size fits the box to the text, so deriving the size back from
    the box reproduces PlanSwift's appearance without depending on Arial metrics
    or the scaled/non-scaled size properties (which don't match our font)."""
    lines = [ln for ln in lines if ln.strip() != ""]
    if not lines or box_w <= 0 or box_h <= 0:
        return 0.0
    n = len(lines)
    avail_w = box_w * (1.0 - 2.0 * NOTE_HPAD_RATIO)
    avail_h = box_h * (1.0 - 2.0 * NOTE_VPAD_RATIO)
    fs_h = avail_h / (n * NOTE_LINE_H)                       # height constraint
    max_w1 = max(fitz.get_text_length(ln, fontname="helv", fontsize=1.0)
                 for ln in lines) or 1.0
    fs_w = avail_w / max_w1                                  # width constraint
    return max(min(fs_h, fs_w), 0.0)


def draw_note(page, box_disp, derot, fill_rgb, border_rgb, text_rgb, lines,
              text_pt, align, fill_alpha, border_pt, oc, h_pad_pt=None):
    """Draw a PlanSwift Note on the shared Notes OCG: a filled + bordered box
    (fill_rgb / border_rgb, either may be None) from box_disp (x0,y0,x1,y1 in
    DISPLAY space) with the note text inside (text_rgb, horizontally aligned,
    vertically centered, multi-line). Everything is transformed to content via
    derot so it reads upright on rotated sheets. align: 0=left 1=center 2=right.
    h_pad_pt is the left/right inset used for alignment (defaults to a small
    fraction of text_pt when not supplied)."""
    x0, y0, x1, y1 = box_disp
    if fill_rgb is not None or border_rgb is not None:
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        cpts = [fitz.Point(x, y) * derot for (x, y) in corners]
        sh = page.new_shape()
        sh.draw_polyline(cpts + [cpts[0]])
        sh.finish(color=border_rgb, fill=fill_rgb,
                  width=border_pt if border_rgb is not None else 0,
                  fill_opacity=fill_alpha, stroke_opacity=1.0, oc=oc)
        sh.commit()
    lines = [ln for ln in lines if ln.strip() != ""]
    if not lines or text_pt <= 0:
        return
    font = fitz.Font("helv")
    pad = h_pad_pt if h_pad_pt is not None else text_pt * 0.15
    line_h = NOTE_LINE_H * text_pt
    cy = (y0 + y1) / 2.0
    o = fitz.Point(0, 0) * derot
    rdc = fitz.Point(1, 0) * derot - o          # readable dir -> content
    rl = abs(rdc) or 1.0
    rcx, rcy = rdc.x / rl, rdc.y / rl
    M = fitz.Matrix(rcx, -rcy, rcy, rcx, 0, 0)
    n = len(lines)
    for i, ln in enumerate(lines):
        lw = fitz.get_text_length(ln, fontname="helv", fontsize=text_pt)
        if align == 1:
            lx = (x0 + x1) / 2.0 - lw / 2.0
        elif align == 2:
            lx = x1 - pad - lw
        else:
            lx = x0 + pad
        # baseline that vertically centers the n-line block in the box
        ly = cy - (n * line_h) / 2.0 + (i + 0.5) * line_h + 0.35 * text_pt
        base = fitz.Point(lx, ly) * derot
        w = fitz.TextWriter(page.rect, color=text_rgb)
        w.append(base, ln, fontsize=text_pt, font=font)
        w.write_text(page, morph=(base, M), oc=oc)


def stop_source_export(out_path, manifest_path, issues):
    text = "Source PDF check failed. No PDF was created or replaced.\n\n"
    text += "\n".join("%s: %s" % issue for issue in issues)
    text += "\n\nManifest retained: " + str(manifest_path)
    with open(str(out_path) + ".report.txt", "w", encoding="utf-8") as report_file:
        report_file.write(text + "\n")
    raise ValueError(text)


def require_complete_preview(skipped):
    if skipped:
        raise ValueError("Preview stopped because some selected pages could not be rendered:\n\n"
                         + "\n".join("%s: %s" % issue for issue in skipped))


def build(manifest_path, strict=False, output_path=None):
    job = parse_manifest(manifest_path)
    out_path = output_path or job.get("OutputPDF") or os.path.join(
        os.path.dirname(manifest_path), "takeoff_export.pdf")
    include_legend = job.get("IncludeLegend", "True").lower() != "false"
    greyscale = job.get("Greyscale", "True").lower() == "true"
    background_mode = job.get("BackgroundMode", "Raster").strip().lower()
    if background_mode not in ("raster", "pdf"):
        raise ValueError("Unknown BackgroundMode: %r" % background_mode)
    source_pdf_mode = job.get("SourcePDFMode", "Files").strip().lower()
    if source_pdf_mode not in ("files", "labels"):
        raise ValueError("Unknown SourcePDFMode: %r" % source_pdf_mode)
    project_root = job.get("ProjectRoot", "").strip() or os.path.dirname(os.path.abspath(out_path))
    if background_mode == "pdf" and "SourceRootJSON" in job:
        source_root = json.loads(job["SourceRootJSON"])
        if not isinstance(source_root, str):
            raise ValueError("Invalid source-folder choice; reopen the export window")
        if source_root:
            if not os.path.isabs(source_root):
                raise ValueError("The source project folder must be an absolute path")
            project_root = source_root

    # Legend type icons (from embedded plugin properties, cached to disk by the
    # Pascal side). Segment reuses the Linear icon; a blank/missing path just
    # means that row gets no icon.
    legend_icons = {
        "area":      job.get("LegendIconArea", "").strip(),
        "wall area": job.get("LegendIconWallArea", "").strip(),
        "linear":    job.get("LegendIconLinear", "").strip(),
        "segment":   job.get("LegendIconLinear", "").strip(),
        "count":     job.get("LegendIconCount", "").strip(),
    }

    def legend_icon_for(it):
        p = legend_icons.get(it, "")
        return p if (p and os.path.isfile(p)) else None

    report = []
    skipped = []

    report.append("BACKGROUND MODE: " + background_mode.upper())
    # Pass 1 — the TIFF supplies tracing pixel dimensions in both modes.
    # PDF mode matches sheet filenames or explicit PDF page labels.
    resolved = []
    source_resolver = SourceResolver(project_root, source_pdf_mode == "labels")
    if background_mode == "pdf":
        report.append("SOURCE PDF MATCHING: " + source_pdf_mode.upper())
        report.append("SOURCE PROJECT FOLDER: " + project_root)

    for page in job["pages"]:
        name = page.get("PageName", "?")
        sheet = clean_sheet_id(name) or (page.get("SheetID") or "").lstrip("*").strip()
        folder = (page.get("PageFolder") or "").strip()
        if not folder:
            skipped.append((name, "no PageFolder in manifest (re-run the exporter)"))
            continue
        tiff, err = resolve_page_tiff(folder)
        if tiff is None:
            skipped.append((name, err))
            continue
        # The TIFF's own pixel size is the authoritative page size for EVERY
        # page — digitizer coordinates live in this image's pixel space, and
        # PlanSwift's [!PageWidth]/[!PageHeight] are merely derived from the
        # same image (they read 0 while its image loader is in a failed state,
        # e.g. 'error (Count <> 1)' after huge 32-bit-colour imports stress
        # the 32-bit process). The exporter no longer asks PlanSwift for them;
        # any values still present in the manifest are only a cross-check and
        # a last resort if the TIFF header itself cannot be read.
        try:
            man_w = float(page.get("PageWidthPx", 0) or 0)
            man_h = float(page.get("PageHeightPx", 0) or 0)
        except (TypeError, ValueError):
            man_w = man_h = 0.0
        dims = tiff_pixel_size(tiff)
        if dims:
            if man_w > 0 and man_h > 0 and \
                    (int(man_w) != dims[0] or int(man_h) != dims[1]):
                report.append("WARNING %s: PlanSwift PageWidth/Height "
                              "%.0f x %.0f differ from the page TIFF "
                              "%d x %d px — using the TIFF (takeoff maps to "
                              "the TIFF's pixel space)"
                              % (sheet or name, man_w, man_h, dims[0], dims[1]))
            page["PageWidthPx"] = str(dims[0])
            page["PageHeightPx"] = str(dims[1])
        # else: keep whatever the manifest carried; if that is also blank the
        # page is reported in Pass 3 (and Pass 2 will likely fail to open the
        # TIFF anyway, which reports it even sooner).
        source_pdf = None
        if background_mode == "pdf":
            source_pdf, err = source_resolver.match(page.get("TakeoffPath", ""), sheet)
            if source_pdf is None:
                skipped.append((name, "source PDF: " + err))
                continue
            if os.path.normcase(os.path.realpath(source_pdf[0])) == os.path.normcase(os.path.realpath(out_path)):
                raise ValueError("Output PDF would overwrite a source PDF; choose a different output name")
            try:
                source_resolver.check(source_pdf, page)
            except Exception as exc:
                skipped.append((name, "source PDF: %s" % exc))
                continue
        resolved.append((page, sheet, tiff, source_pdf))

    if background_mode == "pdf" and skipped:
        stop_source_export(out_path, manifest_path, skipped)

    if strict:
        require_complete_preview(skipped)
        if not resolved:
            raise ValueError("No pages were selected for preview.")

    if not resolved:
        msg = ["No pages could be matched to their %s backgrounds." % background_mode,
               "",
               "Each page's TIFF is read from its storage folder (the page's",
               "'Folder Path'), using the image GUID in that folder's Data.xml.",
               "Source PDF mode follows TakeoffPath under the chosen source project folder.",
               "",
               "=== per-page attempts ==="]
        msg += ["  %s -> %s" % s for s in skipped]
        text = "\n".join(msg)
        print(text)
        # No file left behind. Pause so the message is readable even though the
        # console is set to auto-close on success.
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass
        sys.exit(1)

    # Pass 2 — copy the chosen background into an upright output page. PDF mode
    # uses source paper dimensions; raster mode uses TIFF DPI or its override.
    # Pass 3 maps TIFF pixels into the displayed output size in both cases.
    out = fitz.open()
    page_index = []
    for page, sheet, tiff, source_pdf in resolved:
        if source_pdf:
            try:
                append_source_pdf(out, source_pdf[0], page, greyscale,
                                  source_page=source_pdf[1], expected_label=source_pdf[2])
            except Exception as exc:
                skipped.append((page.get("PageName", sheet), "source PDF: %s" % exc))
                continue
            detail = source_pdf[0]
            if source_pdf[1] is not None:
                detail += " (PDF page %d, label %s)" % (source_pdf[1] + 1, source_pdf[2])
            report.append("SOURCE PDF %s: %s" % (sheet, detail))
            page_index.append((len(out) - 1, page, sheet))
            continue
        try:
            tdoc = fitz.open(tiff)
            trect = tdoc[0].rect
            tdoc.close()
        except Exception as e:
            skipped.append((page.get("PageName", sheet), "cannot open TIFF: %s" % e))
            continue
        w_pt, h_pt = trect.width, trect.height
        if PAGE_DPI_OVERRIDE:
            pw_px = float(page.get("PageWidthPx", 0) or 0)
            ph_px = float(page.get("PageHeightPx", 0) or 0)
            if pw_px > 0 and ph_px > 0:
                w_pt = pw_px * 72.0 / PAGE_DPI_OVERRIDE
                h_pt = ph_px * 72.0 / PAGE_DPI_OVERRIDE
        # Greyscale the client's sheet BEFORE any takeoff is drawn so the colored
        # overlay (Pass 3) stands out. Only pages that carry takeoff are greyed;
        # a page pulled in solely by "Print Page" stays in full color. The image
        # is greyed at insert (raster page has no vector content to recolor).
        pix = None
        if greyscale and page.get("items"):
            try:
                pix = fitz.Pixmap(tiff)
                if (pix.n - pix.alpha) > 1:
                    pix = fitz.Pixmap(fitz.csGRAY, pix)
            except Exception:
                pix = None
        opage = out.new_page(width=w_pt, height=h_pt)
        try:
            if pix is not None:
                opage.insert_image(opage.rect, pixmap=pix)
            else:
                opage.insert_image(opage.rect, filename=tiff)
        except Exception as e:
            report.append("WARNING %s: could not place TIFF: %s" % (sheet, e))
            if strict:
                skipped.append((page.get("PageName", sheet), "cannot place TIFF: %s" % e))
        pix = None
        page_index.append((len(out) - 1, page, sheet))

    if background_mode == "pdf" and skipped:
        out.close()
        stop_source_export(out_path, manifest_path, skipped)

    if not page_index:
        out.close()
        raise ValueError("No backgrounds could be loaded.\n" + "\n".join(
            "%s: %s" % s for s in skipped))

    if strict and skipped:
        out.close()
        require_complete_preview(skipped)

    # One OCG per takeoff item (reused across pages; toggling is global)
    ocg_by_item = {}
    def item_ocg(item):
        key = item.get("ItemGUID") or item.get("ItemName")
        if key not in ocg_by_item:
            ocg_by_item[key] = out.add_ocg(
                LAYER_PREFIX + item.get("ItemName", "Item"), on=True)
        return ocg_by_item[key]

    # Allocate targets before drawing any legends: the first page's Show/Hide
    # all buttons must also control items that only occur on later pages.
    selections_by_page = {}
    selection_streams = []
    selections_by_item = {}
    all_selections = []
    for pno, page, sheet in page_index:
        selections_by_page[pno] = {}
        for item in page['items']:
            base = item_ocg(item)
            if include_legend and base not in selections_by_page[pno]:
                # Unique even for duplicate sheets: PDF optimization must never
                # merge two pages' selection controls into a shared OCG.
                select = out.add_ocg('Selection | Drawing %d | %s | %s' %
                                     (pno + 1, sheet, item.get('ItemName', 'Item')), on=False)
                selections_by_page[pno][base] = select
                all_selections.append(select)
                selections_by_item.setdefault(base, []).append(select)
    all_takeoffs = list(ocg_by_item.values())

    # Legend type-icons are made unique per placement (see _unique_icon) so image
    # dedup can't merge them onto one layer. Cache each icon's base bytes; uid is
    # a running per-placement id.
    icon_cache = {}
    icon_uid = [0]
    dim_ocg = [None]           # shared "Dimensions" OCG, created on first use
    hl_ocg = [None]            # shared "Highlighters" OCG, created on first use
    note_ocg = [None]          # shared "Notes" OCG, created on first use

    # Pass 3 — draw
    inserted_legend_pages = 0
    page_titles = []
    page_keys = []
    for pno, page, sheet in page_index:
        page_titles.append(page.get("PageName") or sheet)
        page_key = page.get("PageGUID") or "@sheet-%d" % pno
        page_keys.append(page_key)
        pdf_page = out[pno + inserted_legend_pages]
        pw = float(page.get("PageWidthPx", 0) or 0)
        ph = float(page.get("PageHeightPx", 0) or 0)
        sx_ft = float(page.get("ScaleX", 0) or 0)   # px per scale-unit
        disp = pdf_page.rect
        if pw <= 0 or ph <= 0:
            skipped.append((page.get("PageName", sheet),
                            "missing PageWidth/Height (and the page TIFF's "
                            "size could not be read)"))
            continue
        sx, sy = disp.width / pw, disp.height / ph
        if abs(sx - sy) / max(sx, sy) > 0.01:
            report.append("WARNING %s: non-uniform scale x=%.5f y=%.5f — page "
                          "TIFF aspect may differ from PageWidth/Height" %
                          (sheet, sx, sy))
        derot = pdf_page.derotation_matrix

        def to_page(x, y, _disp=disp, _sx=sx, _sy=sy, _d=derot):
            return fitz.Point(_disp.x0 + x * _sx, _disp.y0 + y * _sy) * _d

        # Same mapping but WITHOUT derotation — i.e. coordinates in the page's
        # *displayed* (visually upright) space. The legend is laid out here so
        # widths/heights are the visual ones, then `* derot` is applied exactly
        # once at draw time. (to_page above already bakes derot in, so anything
        # built from it must NOT be multiplied by derot again.)
        def to_disp(x, y, _disp=disp, _sx=sx, _sy=sy):
            return fitz.Point(_disp.x0 + x * _sx, _disp.y0 + y * _sy)

        # Highlighters — child items of the page, drawn FIRST so they sit under
        # the takeoff (a highlight marks the sheet; takeoff stays clear on top).
        # Box mode = filled semi-transparent polygon; Linear mode = thick
        # round-capped stroke. On a shared non-toggle "Highlighters" layer, kept
        # out of the legend. Transparency is the opacity (alpha = value/255).
        for hl in page.get("highlighters", []):
            vis = (hl.get("Visible", "True") or "").strip().lower()
            if vis in ("false", "0", "no"):
                continue
            hpts = parse_points(hl.get("digitizer_xml", ""))
            if len(hpts) < 2:
                continue
            hmode = (hl.get("HighlighterMode", "") or "").strip().lower()
            bmode = (hl.get("BoxMode", "") or "").strip().lower()
            if hmode == "line":
                is_linear = True
            elif hmode == "solid":
                is_linear = False
            else:
                is_linear = (bmode == "none")
            try:
                tp = float(hl.get("Transparency", 153) or 153)
            except ValueError:
                tp = 153.0
            alpha = min(max(tp / 255.0, 0.0), 1.0)
            try:
                lw_px = float(hl.get("LineWidth", 0) or 0)
            except ValueError:
                lw_px = 0.0
            lw_pt = lw_px * sx
            pts_disp = [(to_disp(px, py).x, to_disp(px, py).y) for (px, py) in hpts]
            if hl_ocg[0] is None:
                hl_ocg[0] = out.add_ocg("Highlighters", on=True)
            draw_highlighter(pdf_page, pts_disp, derot,
                             tcolor_to_rgb01(hl.get("Color", "")), alpha,
                             is_linear, lw_pt, hl_ocg[0])

        legend_rows = []
        selection_geometry = []
        takeoff_bounds = None
        takeoff_obstacles = []

        def protect_geometry(points, margin=0.0):
            nonlocal takeoff_bounds
            rect = fitz.Rect(min(p.x for p in points) - margin,
                             min(p.y for p in points) - margin,
                             max(p.x for p in points) + margin,
                             max(p.y for p in points) + margin)
            # Input is unrotated page space; layout uses displayed space.
            rect = rect * pdf_page.rotation_matrix
            takeoff_obstacles.append(rect)
            takeoff_bounds = rect if takeoff_bounds is None else takeoff_bounds | rect
        # Draw order = stacking order (later draws sit on top). Areas go down
        # first, then Linear/Segment, then Count markers on top — so counts
        # and lines are never hidden under a filled area.
        # Wall Area is geometrically a LINEAR wall path (length x height gives an
        # area *quantity*, but the drawn shape is a line) — so it lives on the
        # linear layer and is stroked, never filled.
        _z = {"area": 0, "linear": 1, "segment": 1, "wall area": 1, "count": 2}
        draw_items = sorted(
            page["items"],
            key=lambda it: _z.get((it.get("ItemType") or "Linear").lower(), 1))
        for item in draw_items:
            rgb = tcolor_to_rgb01(pdf_item_color(item))
            # A white item Color draws a white (invisible) perimeter on the
            # sheet — fall back to black for the stroke while keeping the fill.
            stroke_rgb = (0.0, 0.0, 0.0) if min(rgb) >= 0.95 else rgb
            oc = item_ocg(item)
            itype = (item.get("ItemType") or "Linear").lower()
            # Fixed stroke width (points) so the weight is identical in every
            # viewer. Area = thin outline; linear / segment / wall-area = solid
            # line.
            width = AREA_PERIMETER_PT if itype == "area" else LINEAR_WIDTH_PT

            open_shape = pdf_page.new_shape()
            closed_shape = pdf_page.new_shape()
            drawn_len_px = 0.0
            n_open = n_closed = 0
            has_holes = False          # any Area Subtract Section on this item
            selection_paths = []
            selection_counts = []

            # A hatched area (Fill Type = Hatch Pattern) keeps its solid Color
            # fill and gets the pattern drawn ON TOP in the Hatch Pattern Color.
            # Detect by a valid parsed .PAT so a FillType wording change can't
            # silently disable it; the exporter only ships a code when hatch is on.
            hatch_pat = item.get("hatch_pat", "") if itype == "area" else ""
            hatch_fams = parse_pat(hatch_pat) if hatch_pat.strip() else []
            is_hatch = itype == "area" and bool(hatch_fams)
            hatch_polys = []

            # Count 'Icon' symbol: resolve the item's Symbol Icon image once, so
            # each count point can be stamped with it instead of a plain circle.
            # The icon is base64 in the item's own Data.xml, located by descending
            # a GUID chain from a bracket-free anchor (both emitted per item).
            count_icon = None
            sym_w_px = sym_h_px = 0.0
            sym_rot = 0
            if itype == "count" and \
                    (item.get("SymbolType", "") or "").strip().lower() == "icon":
                raw, ierr = resolve_symbol_icon(
                    (item.get("SymbolAnchor", "") or "").strip(),
                    (item.get("SymbolChain", "") or "").split())
                if raw:
                    count_icon = icon_pixmap(raw)
                if count_icon is None:
                    report.append("WARNING %s: symbol icon not placed (%s) — "
                                  "using marker" % (item.get("ItemName", "?"),
                                                    ierr or "decode failed"))
                else:
                    def _f(v):
                        try:
                            return float((v or "").strip())
                        except (ValueError, AttributeError):
                            return 0.0
                    sw, sh = _f(item.get("SymbolWidth")), _f(item.get("SymbolHeight"))
                    rw, rh = _f(item.get("SymbolRealW")), _f(item.get("SymbolRealH"))
                    scale_y = (float(page.get("ScaleY", 0) or 0) or sx_ft)
                    if sw > 0 and sh > 0:                    # PlanSwift gave px
                        sym_w_px, sym_h_px = sw, sh
                    elif rw > 0 and rh > 0 and sx_ft > 0:    # real-world ft -> px
                        sym_w_px, sym_h_px = rw * sx_ft, rh * scale_y
                    else:                                    # last resort: native px
                        sym_w_px, sym_h_px = count_icon.width, count_icon.height
                        report.append("WARNING %s: no symbol size from PlanSwift — "
                                      "used the icon's native size"
                                      % item.get("ItemName", "?"))
                    sym_rot = int(round(_f(item.get("SymbolRotate")) / 90.0)) * 90 % 360
                    if sym_w_px <= 0 or sym_h_px <= 0:
                        count_icon = None          # no usable size -> marker

            for sec in item["sections"]:
                pts_px = parse_points(sec.get("digitizer_xml", ""))
                if len(pts_px) < 1:
                    continue
                closed = (sec.get("Closed", "False").lower() == "true"
                          or itype == "area")
                pts = [to_page(x, y) for x, y in pts_px]

                if itype == "count":
                    for p in pts:
                        if count_icon is not None:
                            w_pt = sym_w_px * sx
                            h_pt = sym_h_px * sy
                            r = fitz.Rect(p.x - w_pt / 2.0, p.y - h_pt / 2.0,
                                          p.x + w_pt / 2.0, p.y + h_pt / 2.0)
                            protect_geometry([r.tl, r.br])
                            selection_counts.append(r)
                            pdf_page.insert_image(r, pixmap=count_icon,
                                                  rotate=sym_rot,
                                                  keep_proportion=False, oc=oc)
                        else:
                            protect_geometry([p], COUNT_MARKER_RADIUS_PT + 0.5)
                            selection_counts.append(fitz.Rect(
                                p.x - COUNT_MARKER_RADIUS_PT, p.y - COUNT_MARKER_RADIUS_PT,
                                p.x + COUNT_MARKER_RADIUS_PT, p.y + COUNT_MARKER_RADIUS_PT))
                            open_shape.draw_circle(p, COUNT_MARKER_RADIUS_PT)
                            n_open += 1
                elif len(pts_px) >= 2:
                    selection_paths.append((pts, closed))
                    protect_geometry(pts, width / 2.0)
                    drawn_len_px += polyline_len(pts_px, closed)
                    if closed:
                        # Close explicitly by repeating the first point. PyMuPDF's
                        # finish(closePath=True) only closes the LAST subpath when
                        # a shape holds several polylines, so a multi-section area
                        # (or a section plus its deducts) would drop the closing
                        # edge of every earlier subpath.
                        closed_shape.draw_polyline(pts + pts[:1])
                        n_closed += 1
                    else:
                        open_shape.draw_polyline(pts)
                        n_open += 1

                # Area deducts: punch each subtract polygon out of this section's
                # fill. They go into the SAME closed shape; the even-odd rule at
                # finish turns the overlap into a hole and outlines its boundary.
                sub_rings = []
                for sub in sec.get("subtracts", []):
                    spts_px = parse_points(sub.get("digitizer_xml", ""))
                    if len(spts_px) >= 3:
                        spts = [to_page(x, y) for x, y in spts_px]
                        selection_paths.append((spts, True))
                        protect_geometry(spts, width / 2.0)
                        closed_shape.draw_polyline(spts + spts[:1])
                        n_closed += 1
                        has_holes = True
                        if is_hatch:
                            sub_rings.append([(to_disp(x, y).x, to_disp(x, y).y)
                                              for x, y in spts_px])

                # A hatched section contributes its boundary + its deducts (as
                # holes) to the hatch pass.
                if is_hatch and closed and len(pts_px) >= 3:
                    ext = [(to_disp(x, y).x, to_disp(x, y).y) for x, y in pts_px]
                    hatch_polys.append([ext] + sub_rings)

            # closePath is finish()'s default (True) — open polylines MUST
            # pass closePath=False or every figure gets closed back to start.
            if n_open:
                if itype == "count":
                    open_shape.finish(color=stroke_rgb, fill=rgb, width=1.0,
                                      fill_opacity=0.9, oc=oc)
                else:
                    open_shape.finish(color=stroke_rgb, width=width, lineCap=1,
                                      lineJoin=1, stroke_opacity=STROKE_OPACITY,
                                      closePath=False, oc=oc)
            if n_closed:
                fill = rgb if itype == "area" else None
                # Polygons are already closed explicitly above, so closePath is
                # off (PyMuPDF only honors it for the last subpath anyway).
                # even_odd only when deducts exist, so plain areas (including any
                # overlapping sections) keep their nonzero-winding union fill.
                closed_shape.finish(color=stroke_rgb, width=width, lineCap=1,
                                    lineJoin=1, stroke_opacity=STROKE_OPACITY,
                                    fill=fill, fill_opacity=AREA_FILL_OPACITY,
                                    closePath=False, even_odd=has_holes, oc=oc)
            open_shape.commit()
            closed_shape.commit()

            # Hatch pass: pattern lines clipped to each section, in the Hatch
            # Pattern Color, on the item's layer (after greyscale -> stays color).
            if is_hatch and hatch_polys:
                try:
                    hscale = float(item.get("HatchScale", "1") or 1)
                except ValueError:
                    hscale = 1.0
                hcol = (item.get("HatchColor") or "").strip() or pdf_item_color(item)
                hrgb = tcolor_to_rgb01(hcol)
                # real-world model: .PAT unit x scale x points-per-foot. Swap
                # (sx_ft * sx) for 72 if the pattern turns out to be paper-inches.
                unit_pts = hscale * (sx_ft * sx) * HATCH_SCALE_ADJUST
                for rings in hatch_polys:
                    draw_hatch(pdf_page, rings, hatch_fams, unit_pts, hrgb, oc, derot)

            # Calculated Qty may represent a different measure than the geometry.
            total = item.get("PageTotal", "")
            units = item.get("Units", "")
            if item.get("PageTotalBasis", "").strip().lower() == "qty":
                report.append("%-12s %-40s allocated Qty %s %s" %
                              (sheet, item.get("ItemName", "")[:40], total, units))
                if not units.strip():
                    report.append("WARNING %s: Qty inputUnits missing for %s; "
                                  "legend unit left blank" %
                                  (sheet, item.get("ItemName", "?")))
            elif sx_ft > 0 and itype in ("linear", "segment") and total:
                try:
                    computed = drawn_len_px / sx_ft
                    delta = computed - float(total)
                    report.append("%-12s %-40s computed %10.2f  planswift %10s "
                                  "%s  delta %+8.3f" %
                                  (sheet, item.get("ItemName", "")[:40],
                                   computed, total, units, delta))
                except ValueError:
                    pass
            legend_rows.append((item, rgb, ceil_qty_str(total), units, oc,
                                itype, count_icon))
            if include_legend:
                select = selections_by_page[pno][oc]
                selection_geometry.append((selection_paths, selection_counts, select, oc))

        # All selection overlays sit above ordinary takeoff geometry, including
        # overlapping items, and below dimensions, notes, and the legend.
        for paths, counts, selected_oc, base in selection_geometry:
            streams = draw_selection_overlay(pdf_page, paths, counts, selected_oc)
            if streams:
                selection_streams.append((pdf_page.xref, base, streams))

        # Dimensions — child items of the page, drawn as annotations (not takeoff
        # quantities): line + witness lines + arrowheads + a scaled feet-inches
        # label, all on one shared non-toggle "Dimensions" layer and kept out of
        # the legend. Value uses ScaleX/ScaleY with an X-or-Y fallback (PlanSwift
        # assumes the missing axis equals the set one on custom-scaled sheets).
        scale_x = float(page.get("ScaleX", 0) or 0)
        scale_y = float(page.get("ScaleY", 0) or 0)
        sxe = scale_x or scale_y
        sye = scale_y or scale_x
        for dim in page.get("dimensions", []):
            dpts = parse_points(dim.get("digitizer_xml", ""))
            if len(dpts) < 2 or sxe <= 0 or sye <= 0:
                continue
            (x1, y1), (x2, y2) = dpts[0], dpts[-1]
            length_ft = math.hypot((x2 - x1) / sxe, (y2 - y1) / sye)
            try:
                precision = int(round(float(dim.get("Precision", 16) or 16)))
            except ValueError:
                precision = 16

            def _flag(name, default="True"):
                return (dim.get(name, default) or "").strip().lower() == "true"

            label = format_feet_inches(length_ft, precision,
                                       _flag("Fractions"), _flag("Inches"),
                                       _flag("Dashes"))
            try:
                th = float(dim.get("TextHeight", 1) or 1)
            except ValueError:
                th = 1.0
            text_pt = th * (sxe * sx) * TEXT_HEIGHT_ADJUST
            if text_pt <= 0:
                continue
            if dim_ocg[0] is None:
                dim_ocg[0] = out.add_ocg("Dimensions", on=True)
            line_rgb = tcolor_to_rgb01(dim.get("Color", ""))
            tc = (dim.get("TextColor", "") or "").strip()
            text_rgb = tcolor_to_rgb01(tc) if tc else line_rgb
            draw_dimension(pdf_page, to_disp(x1, y1), to_disp(x2, y2), derot,
                           line_rgb, text_rgb,
                           label, text_pt, _flag("Arrowheads"), dim_ocg[0])

        # Notes — child items of the page, drawn on top (an opaque text box that
        # marks the sheet): filled + bordered box with the note text inside. On a
        # shared non-toggle "Notes" layer, kept out of the legend. Text Height is
        # the scaled height in real units (like dimensions) when Scaled=True.
        n_scale_x = float(page.get("ScaleX", 0) or 0)
        n_scale_y = float(page.get("ScaleY", 0) or 0)
        n_sxe = n_scale_x or n_scale_y
        for nt in page.get("notes", []):
            nvis = (nt.get("Visible", "True") or "").strip().lower()
            if nvis in ("false", "0", "no"):
                continue
            npts = parse_points(nt.get("digitizer_xml", ""))
            if len(npts) < 2:
                continue
            nxs = [p[0] for p in npts]
            nys = [p[1] for p in npts]
            box_disp = (to_disp(min(nxs), min(nys)).x, to_disp(min(nxs), min(nys)).y,
                        to_disp(max(nxs), max(nys)).x, to_disp(max(nxs), max(nys)).y)
            box_w = box_disp[2] - box_disp[0]
            box_h = box_disp[3] - box_disp[1]
            lines = (nt.get("Text", "") or "").split("[[NL]]")
            # PlanSwift Auto Size fits the box to the text, so fit the text back
            # to the box (this reproduces the appearance without matching Arial
            # metrics). Only when Auto Size is off do we honour the size props,
            # and even then cap to the box so text can't bleed out.
            fit_pt = fit_note_text_pt(lines, box_w, box_h)
            autosize = (nt.get("AutoSize", "True") or "").strip().lower() != "false"
            if autosize:
                ntext_pt = fit_pt
            else:
                scaled = (nt.get("Scaled", "True") or "").strip().lower() == "true"
                try:
                    if scaled:
                        th_n = float(nt.get("TextHeight", 0) or 0)
                        intended = th_n * (n_sxe * sx) * NOTE_TEXT_ADJUST
                    else:
                        ts_n = float(nt.get("TextSize", 0) or 0)
                        intended = ts_n * sx * NOTE_TEXT_ADJUST
                except ValueError:
                    intended = fit_pt
                ntext_pt = min(intended, fit_pt) if fit_pt > 0 else intended
            try:
                ntp = float(nt.get("Transparency", 1) or 1)
            except ValueError:
                ntp = 1.0
            nalpha = ntp if ntp <= 1.0 else ntp / 255.0
            nalpha = min(max(nalpha, 0.0), 1.0)
            al = (nt.get("TextAlignment", "Left") or "").strip().lower()
            align = 1 if al == "center" else (2 if al == "right" else 0)
            cfill = (nt.get("Color", "") or "").strip()
            cbord = (nt.get("BorderColor", "") or "").strip()
            ctext = (nt.get("TextColor", "") or "").strip()
            if note_ocg[0] is None:
                note_ocg[0] = out.add_ocg("Notes", on=True)
            draw_note(pdf_page, box_disp, derot,
                      tcolor_to_rgb01(cfill) if cfill else None,
                      tcolor_to_rgb01(cbord) if cbord else None,
                      tcolor_to_rgb01(ctext) if ctext else (0, 0, 0),
                      lines, ntext_pt, align, nalpha, NOTE_BORDER_PT,
                      note_ocg[0], h_pad_pt=box_w * NOTE_HPAD_RATIO)

        # Legend — white box anchored at a TOP CORNER of the PlanSwift 'Legend'
        # Legend — anchored at the PlanSwift Legend box's top corner, using its
        # WIDTH. Text is sized so the WIDEST row fills that width; the box then
        # grows DOWN to hold all rows. Only if it would pass the page bottom do
        # we shrink the text to fit the remaining space.
        drawing_page_number = pdf_page.number
        legend_on_separate_page = False
        drawing_legend_box = None
        if include_legend and legend_rows:
            # Alphabetical by item name (like PlanSwift "Sort by Name").
            legend_rows.sort(key=lambda r: r[0].get("ItemName", "").lower())
            legend_pts = parse_points(page.get("legend_xml", ""))
            n = len(legend_rows)
            pad = 8.0
            # A type icon sits at the left of every row when we have any icons.
            any_icon = any(legend_icon_for(r[5]) for r in legend_rows)
            any_symbol = any(r[6] is not None for r in legend_rows)

            # --- box top-left corner + WIDTH from the Legend polygon ---
            # Built in DISPLAY space (to_disp, no derot) so widths/heights are
            # the visual ones on a rotated page. `* derot` is applied once at
            # draw time below.
            is_fallback = len(legend_pts) < 2
            report.append("LEGEND %s: mode=%s; drawing PDF page=%d; "
                          "page size=%.1f x %.1f pt; tracing bounds=%s; rows=%d" %
                          (sheet, "automatic" if is_fallback else "PlanSwift-box",
                           drawing_page_number + 1, disp.width, disp.height,
                           str(takeoff_bounds), n))
            if not is_fallback:
                xs = [p[0] for p in legend_pts]
                ys = [p[1] for p in legend_pts]
                cA = to_disp(min(xs), min(ys))
                cB = to_disp(max(xs), max(ys))
                box_w = abs(cB.x - cA.x)
                top = min(cA.y, cB.y)
                left = min(cA.x, cB.x)
                report.append("LEGEND %s: supplied Legend bounds in page pixels="
                              "(%.2f, %.2f, %.2f, %.2f); mapped left=%.2f, "
                              "top=%.2f, width=%.2f pt" %
                              (sheet, min(xs), min(ys), max(xs), max(ys),
                               left, top, box_w))

            inner_w = box_w - 2 * pad - 12 if not is_fallback else 0.0

            # --- font size so the WIDEST row fills the box width ---
            # A row is: [eye] [optional type icon] [swatch] [qty bold] [name]
            # The drawing uses a shared qty column sized to the WIDEST qty, so
            # the estimate must too: name starts after swatch+gap+max_qty+gap.
            def max_qty_at(fs):
                return max(fitz.get_text_length(
                    "%s %s" % (r[2], r[3]),
                    fontname=LEGEND_FONT_BOLD, fontsize=fs)
                    for r in legend_rows)

            def max_name_at(fs):
                return max(fitz.get_text_length(
                    r[0].get("ItemName", ""), fontname=LEGEND_FONT, fontsize=fs)
                    for r in legend_rows)

            def row_width_at(fs):
                cell = (fs * LEGEND_SYMBOL_W_MULT) if any_symbol else (fs * 1.15)
                w = (fs * 1.65) + cell + (fs * 0.5) + max_qty_at(fs) \
                    + (fs * 0.5) + max_name_at(fs)
                if any_icon:                       # type-icon square + its gap
                    w += (fs * 1.15) + (fs * 0.5)
                return w

            button_labels = ('Hide all', 'Show all', 'Clear highlight')

            def toolbar_width(fs):
                return sum(fitz.get_text_length(label, fontname=LEGEND_FONT, fontsize=fs * .7)
                           + fs * .8 for label in button_labels) + fs * .8

            def title_height(fs):
                return fs + 8 + fs * 1.5 + 4

            def legend_height(fs):
                rh = max(fs * 1.7, fs * LEGEND_SYMBOL_H_MULT + 2 if any_symbol else 0)
                return 2 * pad + title_height(fs) + n * rh

            if is_fallback:
                # Measure the title as well as the rows. The entire enclosing
                # tracing rectangle is protected, including blank gaps inside.
                def auto_width(fs):
                    title_w = fitz.get_text_length("TAKEOFF SUMMARY - " + sheet,
                                                  fontname=LEGEND_FONT_BOLD,
                                                  fontsize=fs)
                    return max(row_width_at(fs) + 12, title_w, toolbar_width(fs)) + 2 * pad

                def auto_height(fs):
                    return legend_height(fs)

                layout, placement_rule = adaptive_legend_layout(
                    disp, takeoff_bounds, n, auto_width, auto_height)
                font_sz = MIN_FONT
                if layout is not None:
                    font_sz = layout[0]
                    report.append("%s: automatic legend %.1f pt; rule=%s"
                                  % (sheet, font_sz, placement_rule))
                else:
                    # Never let a compact fallback cross the rightmost tracing.
                    # Keep the complete legend on a following page instead.
                    pdf_page = out.new_page(
                        pno=drawing_page_number + 1,
                        width=max(disp.width, auto_width(MIN_FONT) + 40),
                        height=max(disp.height, auto_height(MIN_FONT) + 30))
                    disp = pdf_page.rect
                    derot = pdf_page.derotation_matrix
                    legend_on_separate_page = True
                    inserted_legend_pages += 1
                    page_titles.append((page.get("PageName") or sheet) + " - Legend")
                    page_keys.append(page_key)
                    layout = automatic_legend_layout(disp, None, auto_width, auto_height)
                    font_sz = layout[0]
                    report.append("WARNING %s: right-hand strip cannot fit legend "
                                  "padding; complete legend placed on the following page."
                                  % sheet)
                placed_box = layout[1]
                box_w = placed_box.width
                left, top = placed_box.x0, placed_box.y0
                inner_w = box_w - 2 * pad - 12
            else:
                # Grow the font until the widest row just fills inner_w.
                fs = MIN_FONT
                step = 0.5
                best = fs
                while fs <= 60.0:
                    if max(row_width_at(fs), toolbar_width(fs)) <= inner_w:
                        best = fs
                        fs += step
                    else:
                        break
                font_sz = best

            # Row/box metrics at that font size
            title_h = title_height(font_sz)
            row_h = max(font_sz * 1.7,
                        (font_sz * LEGEND_SYMBOL_H_MULT + 2) if any_symbol else 0)
            box_h = pad + title_h + n * row_h + pad

            # --- clamp to page bottom: only NOW do we shrink text if needed ---
            avail_h = (disp.y1 - 10) - top
            if box_h > avail_h and n > 0:
                usable = avail_h - pad - pad
                # solve row_h from usable = title_h + n*row_h, with title_h
                # tied to font; approximate by scaling font down proportionally
                shrink = avail_h / box_h
                font_sz = max(5.0, font_sz * shrink)
                title_h = title_height(font_sz)
                row_h = max(font_sz * 1.7,
                            (font_sz * LEGEND_SYMBOL_H_MULT + 2) if any_symbol else 0)
                box_h = pad + title_h + n * row_h + pad

            swatch = font_sz * 1.15
            sym_cell_w = (font_sz * LEGEND_SYMBOL_W_MULT) if any_symbol else swatch
            sym_cell_h = font_sz * LEGEND_SYMBOL_H_MULT
            box = fitz.Rect(left, top, left + box_w, top + box_h)
            if not legend_on_separate_page:
                drawing_legend_box = fitz.Rect(box)
            report.append("LEGEND %s: final PDF page=%d; box=%s; font=%.2f pt%s" %
                          (sheet, pdf_page.number + 1, str(box), font_sz,
                           "; separate legend page" if legend_on_separate_page else ""))
            if not disp.contains(box):
                report.append("WARNING %s: legend box extends beyond page bounds" % sheet)

            # White box + thin border
            boxshape = pdf_page.new_shape()
            boxshape.draw_rect(box * derot)
            boxshape.finish(color=(0, 0, 0), fill=(1, 1, 1),
                            fill_opacity=1.0, width=0.8)
            boxshape.commit()

            inner_x = box.x0 + pad
            # Title
            tp = fitz.Point(inner_x, box.y0 + pad + font_sz) * derot
            pdf_page.insert_text(tp, "TAKEOFF SUMMARY - " + sheet,
                                 fontname=LEGEND_FONT_BOLD,
                                 fontsize=font_sz, color=(0, 0, 0),
                                 rotate=pdf_page.rotation)

            # A compact control row inside the legend header. These links are
            # always visible and only target takeoff/selection OCGs.
            button_x = inner_x
            button_y = box.y0 + pad + font_sz + 5
            button_scale = min(1.0, (box_w - 2 * pad) / toolbar_width(font_sz))
            control_fs = font_sz * button_scale
            actions = (
                [('OFF', all_takeoffs + all_selections)],
                [('ON', all_takeoffs), ('OFF', all_selections)],
                [('OFF', list(selections_by_page[pno].values()))],
            )
            for label, commands in zip(button_labels, actions):
                button_w = fitz.get_text_length(label, fontname=LEGEND_FONT,
                                                fontsize=control_fs * .7) + control_fs * .8
                button_box = fitz.Rect(button_x, button_y, button_x + button_w,
                                       button_y + font_sz * 1.3)
                shape = pdf_page.new_shape()
                shape.draw_rect(button_box * derot)
                shape.finish(color=(.55, .55, .55), fill=(.96, .96, .96), width=.5)
                shape.commit()
                baseline = fitz.Point(button_x + control_fs * .4,
                                      button_y + font_sz * .65 + control_fs * .24) * derot
                pdf_page.insert_text(baseline, label, fontsize=control_fs * .7,
                                     fontname=LEGEND_FONT, color=(.15, .15, .15),
                                     rotate=pdf_page.rotation)
                add_layer_action(pdf_page, button_box, derot, label, commands)
                button_x += button_w + control_fs * .4

            row_top = box.y0 + pad + title_h

            # Column layout: [eye] [type icon] [swatch] [qty+unit bold] [name],
            # gap ~ 0.5*font. The icon column is present only if we have icons.
            gap = font_sz * 0.5
            icon_sz = swatch
            eye_col = icon_sz + gap
            icon_col = (icon_sz + gap) if any_icon else 0.0
            qty_w = 0.0
            for lr in legend_rows:
                qty_w = max(qty_w, fitz.get_text_length(
                    "%s %s" % (lr[2], lr[3]),
                    fontname=LEGEND_FONT_BOLD, fontsize=font_sz))
            swatch_x = inner_x + eye_col + icon_col
            qty_col_x = swatch_x + sym_cell_w + gap
            name_col_x = qty_col_x + qty_w + gap
            name_avail = (box.x1 - pad) - name_col_x

            def fit_name(name):
                """Trim with an ellipsis if the name can't fit its column."""
                if fitz.get_text_length(name, fontname=LEGEND_FONT,
                                        fontsize=font_sz) <= name_avail:
                    return name
                ell = "..."
                lo, hi = 0, len(name)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    w = fitz.get_text_length(name[:mid] + ell,
                                             fontname=LEGEND_FONT,
                                             fontsize=font_sz)
                    if w <= name_avail:
                        lo = mid
                    else:
                        hi = mid - 1
                return (name[:lo] + ell) if lo > 0 else ""

            for i, (item, rgb, total, units, oc, itype, sym) in \
                    enumerate(legend_rows):
                ry = row_top + i * row_h
                if any_symbol:                       # taller rows -> center content
                    baseline_y = ry + row_h / 2.0 + font_sz * 0.35
                else:                                # original top-aligned layout
                    baseline_y = ry + swatch - font_sz * 0.2

                select = selections_by_page[pno][oc]
                row_selection = pdf_page.new_shape()
                row_selection.draw_rect(fitz.Rect(inner_x - 2, ry - 1,
                                                  box.x1 - pad + 2, ry + row_h - 1) * derot)
                row_selection.finish(color=None, fill=(.82, .95, 1), oc=select)
                row_selection.commit()
                selection_streams.append((pdf_page.xref, oc, [pdf_page.get_contents()[-1]]))

                eye_y = (ry + (row_h - icon_sz) / 2.0) if any_symbol else ry
                draw_legend_layer_toggle(
                    pdf_page, fitz.Rect(inner_x, eye_y, inner_x + icon_sz, eye_y + icon_sz),
                    fitz.Rect(inner_x - gap / 4, ry, inner_x + icon_sz + gap / 4, ry + row_h),
                    derot, oc, item.get("ItemName", ""), selections_by_item.get(oc, []))

                # Keep the legend row visible when its takeoff is hidden so the
                # user can identify the layer and turn it back on. Each placement
                # gets a unique (imperceptibly-tweaked) copy so image dedup can't
                # merge same-type icons onto one layer.
                if any_icon:
                    ipath = legend_icon_for(itype)
                    if ipath:
                        iy = (ry + (row_h - icon_sz) / 2.0) if any_symbol else ry
                        irect = fitz.Rect(inner_x + eye_col, iy,
                                          inner_x + eye_col + icon_sz, iy + icon_sz) * derot
                        try:
                            base = icon_cache.get(ipath)
                            if base is None:
                                with open(ipath, "rb") as f:
                                    base = f.read()
                                icon_cache[ipath] = base
                            stream = _unique_icon(base, icon_uid[0])
                            icon_uid[0] += 1
                            pdf_page.insert_image(irect, stream=stream,
                                                  keep_proportion=True,
                                                  rotate=pdf_page.rotation)
                        except Exception:
                            pass

                if sym is not None:
                    # Count 'Icon' symbol: the icon replaces the color swatch,
                    # fitted (aspect preserved) and centered in the symbol cell.
                    nw, nh = float(sym.width), float(sym.height)
                    scale = min(sym_cell_w / nw, sym_cell_h / nh) if nw and nh else 0
                    fw, fh = nw * scale, nh * scale
                    fx = swatch_x + (sym_cell_w - fw) / 2.0
                    fy = ry + (row_h - fh) / 2.0
                    srect = fitz.Rect(fx, fy, fx + fw, fy + fh) * derot
                    try:
                        pdf_page.insert_image(srect, pixmap=sym,
                                              keep_proportion=True,
                                              rotate=pdf_page.rotation)
                    except Exception:
                        pass
                else:
                    # swatch — match the item's on-sheet transparency; centered
                    # (vertically) in the cell, left-aligned at the cell start.
                    sy0 = (ry + (row_h - swatch) / 2.0) if any_symbol else ry
                    sw = pdf_page.new_shape()
                    r = fitz.Rect(swatch_x, sy0,
                                  swatch_x + swatch, sy0 + swatch) * derot
                    sw.draw_rect(r)
                    sw_opacity = AREA_FILL_OPACITY if itype == "area" else 1.0
                    sw.finish(color=(0, 0, 0), fill=rgb, width=0.7,
                              fill_opacity=sw_opacity)
                    sw.commit()

                # qty + unit (bold)
                qty = "%s %s" % (total, units)
                tpq = fitz.Point(qty_col_x, baseline_y) * derot
                pdf_page.insert_text(tpq, qty, fontname=LEGEND_FONT_BOLD,
                                     fontsize=font_sz, color=(0, 0, 0),
                                     rotate=pdf_page.rotation)

                # name (regular) — trimmed so it can never bleed out of the box
                name = fit_name(item.get("ItemName", ""))
                tpn = fitz.Point(name_col_x, baseline_y) * derot
                pdf_page.insert_text(tpn, name, fontname=LEGEND_FONT,
                                     fontsize=font_sz, color=(0, 0, 0),
                                     rotate=pdf_page.rotation)
                # Turn off the other selections on this drawing page, then
                # toggle this one. Re-clicking clears it. Reveal a hidden base
                # takeoff first; other pages keep their own selection state.
                add_layer_action(
                    pdf_page, fitz.Rect(name_col_x, ry, box.x1 - pad, ry + row_h), derot,
                    'Highlight on this page: ' + item.get('ItemName', ''),
                    [('ON', [oc]),
                     ('OFF', [s for s in selections_by_page[pno].values() if s != select]),
                     ('Toggle', [select])])

        if legend_on_separate_page:
            # Adding a page invalidates previous PyMuPDF page objects.
            pdf_page = out[drawing_page_number]
            disp = pdf_page.rect
            derot = pdf_page.derotation_matrix

        # Place the multiplier after final legend layout so it can avoid both
        # manual/automatic legends and individual exported takeoff sections.
        if page.get("items"):
            try:
                obstacles = takeoff_obstacles + ([drawing_legend_box]
                                                 if drawing_legend_box is not None else [])
                badge_box = draw_multiplier_badge(
                    pdf_page, disp, derot, page.get("PageMultiplier", "1"), obstacles)
                report.append("MULTIPLIER %s: box=%s" % (sheet, badge_box))
            except Exception as e:
                report.append("WARNING %s: multiplier badge failed: %s"
                              % (sheet, e))

    if strict and skipped:
        out.close()
        require_complete_preview(skipped)

    gate_selection_streams(out, selection_streams)
    configure_selection_layers(out, all_selections)

    # Optimize before saving: dedupe shared objects across pages (garbage=4 —
    # important since sheets from the same drawing set repeat fonts/images),
    # compress streams/images/fonts, and strip anything no longer referenced.
    # This does NOT rasterize or flatten vector content — geometry and text
    # stay vector and searchable; only the file's internal storage shrinks.
    out.subset_fonts()
    out.scrub(attached_files=True, embedded_files=True, javascript=True,
             metadata=False, xml_metadata=False, remove_links=False,
             thumbnails=True, redactions=False, hidden_text=False,
             reset_fields=True, reset_responses=True)
    out.save(out_path, garbage=4, clean=True, deflate=True,
             deflate_images=True, deflate_fonts=True)
    out.close()

    # Greyscaling re-encodes the sheet's JPEG photos (e.g. a cover render) as
    # lossless Flate, which can multiply an image-heavy page's size. Recompress
    # those greyscaled rasters back to JPEG (soft masks preserved).
    if greyscale:
        shrink_greyscaled_rasters(out_path)

    # Print diagnostics, but remove the sidecars after a complete export.
    lines = ["Saved: " + out_path, "",
             "=== QUANTITIES / VALIDATION ==="]
    lines += report or ["(no linear items with page scale to validate)"]
    if skipped:
        lines += ["", "=== SKIPPED PAGES ==="]
        lines += ["  %s -> %s" % s for s in skipped]
    report_path = str(out_path) + ".report.txt"
    if skipped:
        # An incomplete export still needs its inputs for troubleshooting.
        lines.append("Manifest retained for incomplete export: " + str(manifest_path))
        try:
            with open(report_path, "w", encoding="utf-8") as report_file:
                report_file.write("\n".join(lines) + "\n")
        except OSError as exc:
            lines.append("WARNING: could not save report: %s" % exc)
    else:
        for diagnostic_path in (manifest_path, report_path):
            try:
                os.remove(diagnostic_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                lines.append("WARNING: could not remove %s: %s" % (diagnostic_path, exc))
    print("\n".join(lines))

    # If any page was dropped, warn loudly — a "successful" run must not quietly
    # ship a set that's missing pages.
    warn_missing_sources(skipped)
    return {"page_titles": page_titles, "page_keys": page_keys,
            "warnings": [line for line in report if line.startswith("WARNING")]}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python pdf_takeoff_builder.py <manifest.txt>")
    manifest_arg = sys.argv[1]
    try:
        build(manifest_arg)
    except SystemExit:
        raise
    except Exception:
        import traceback
        print("PDF builder crashed:\n" + traceback.format_exc())
        # No file left behind — pause so the error is readable before close.
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass
        sys.exit(1)
