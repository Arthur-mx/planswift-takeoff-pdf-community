# SPDX-License-Identifier: AGPL-3.0-only
"""Native PlanSwift page annotations (community exporter).

Geometry is inferred from native print/control-point samples, not Live mode.
All coordinates enter in raster pixels and use the exporter's page transform.
"""
import math
import os
import xml.etree.ElementTree as ET
from functools import lru_cache

import fitz

LAYER_NAMES = {"RevisionCloud": "Revision Clouds", "RubberStamp": "Rubber Stamps",
               "StickyTab": "Sticky Tabs"}


def truth(value, default=True):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in ("false", "0", "no")


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def points_from_xml(xml):
    root = ET.fromstring(xml)
    points = [fitz.Point(float(p.attrib["X"]), float(p.attrib["Y"]))
              for p in root.iter("Point")]
    if not all(math.isfinite(v) for p in points for v in p):
        raise ValueError("non-finite control point")
    return points


def cloud_curves(points):
    """Six scallops along each midpoint/vertex/midpoint quadratic corner.

    Two quarter-circle cubics form each outward semicircle. Construct in the
    native pixel space so a nonuniform page transform also transforms controls.
    """
    points = list(points)
    if len(points) > 1 and abs(points[-1] - points[0]) < 1e-6:
        points.pop()
    if len(points) < 3:
        raise ValueError("a cloud requires at least three distinct points")
    area = sum(a.x * b.y - b.x * a.y
               for a, b in zip(points, points[1:] + points[:1]))
    if abs(area) < 1e-6:
        raise ValueError("cloud points do not enclose an area")
    sign = 1 if area > 0 else -1
    curves = []
    k = 0.5522847498307936
    for i, control in enumerate(points):
        start = (points[i - 1] + control) / 2
        end = (control + points[(i + 1) % len(points)]) / 2
        samples = []
        for j in range(7):
            t = j / 6
            samples.append(start * ((1 - t) ** 2) + control * (2 * t * (1 - t))
                           + end * (t * t))
        for a, b in zip(samples, samples[1:]):
            length = abs(b - a)
            if length < 1e-6:
                continue
            u = (b - a) / length
            normal = fitz.Point(u.y, -u.x) * sign
            radius = length / 2
            apex = (a + b) / 2 + normal * radius
            curves.extend(((a, a + normal * (k * radius), apex - u * (k * radius), apex),
                           (apex, apex + u * (k * radius), b + normal * (k * radius), b)))
    return curves


@lru_cache(maxsize=8)
def label_font(kind):
    filename = {"stamp": "impact.ttf", "stencil": "STENCIL.TTF", "tab": "arialbd.ttf"}[kind]
    path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", filename)
    if os.path.isfile(path):
        return fitz.Font(fontfile=path), False
    return fitz.Font("hebo"), True


def draw_text(page, center, direction, text, size, font, color, derot, oc):
    """Centered, searchable text following an arbitrary displayed angle."""
    direction = fitz.Point(direction)
    if direction.x < 0 or (abs(direction.x) < 1e-8 and direction.y > 0):
        direction = -direction
    down = fitz.Point(-direction.y, direction.x)
    lines = str(text).replace("[[NL]]", "\n").splitlines() or [""]
    for index, line in enumerate(lines):
        if not line:
            continue
        line_center = center + down * ((index - (len(lines) - 1) / 2) * size * 1.15)
        baseline = (line_center - direction * (font.text_length(line, fontsize=size) / 2)
                    + down * ((font.ascender + font.descender) * size / 2)) * derot
        vector = (direction * derot) - (fitz.Point(0, 0) * derot)
        vector /= abs(vector)
        morph = fitz.Matrix(vector.x, -vector.y, vector.y, vector.x, 0, 0)
        writer = fitz.TextWriter(page.rect, color=color)
        writer.append(baseline, line, font=font, fontsize=size)
        writer.write_text(page, morph=(baseline, morph), oc=oc)


def render_page_annotations(page, records, to_disp, derot, layers, color_parser,
                            include_hidden=False):
    """Draw native annotations. Return content-space protection bounds and warnings.

    Defaults match the supplied native samples. Non-default transparency and
    stamp autosizing remain approximations and are reported explicitly.
    """
    protected, warnings = [], []
    for record in sorted(records, key=lambda r: (number(r.get("ZOrder")),
                                                number(r.get("OrderIndex")))):
        kind = record.get("NativeType", "")
        name = record.get("Name") or kind or "Annotation"
        if not include_hidden and not truth(record.get("Visible")):
            continue
        if kind not in LAYER_NAMES:
            warnings.append(f"{name}: unsupported annotation type {kind!r}; skipped.")
            continue
        try:
            raw = points_from_xml(record.get("digitizer_xml", ""))
            if kind == "RevisionCloud":
                curves = cloud_curves(raw)
            elif len(raw) != 2 or abs(raw[1] - raw[0]) < 1e-6:
                raise ValueError("exactly two different control points are required")
        except (ValueError, KeyError, ET.ParseError) as exc:
            warnings.append(f"{name}: invalid annotation geometry ({exc}); skipped.")
            continue
        oc = layers.get(kind)
        if oc is None:
            oc = layers[kind] = page.parent.add_ocg(LAYER_NAMES[kind], on=True)
        transparency = number(record.get("Transparency"), 200)
        if kind != "RevisionCloud" and transparency != 200:
            warnings.append(f"{name}: transparency {transparency:g} is approximated; native reference uses 200.")
        alpha_scale = max(0, min(1.275, transparency / 200))
        mapped = [to_disp(p.x, p.y) for p in raw]
        content_points = []

        def polygon(vertices, color, fill, opacity, width):
            content = [p * derot for p in vertices]
            shape = page.new_shape()
            shape.draw_polyline(content + [content[0]])
            shape.finish(color=color, fill=fill, width=width, closePath=True,
                         fill_opacity=opacity, oc=oc)
            shape.commit()
            content_points.extend(content)

        if kind == "RevisionCloud":
            color = color_parser(record.get("BorderColor") or "255")
            # Export clouds as outlines only; leave the drawing beneath clear.
            shape = page.new_shape()
            for curve in curves:
                controls = [to_disp(p.x, p.y) * derot for p in curve]
                shape.draw_bezier(*controls)
                content_points.extend(controls)
            shape.finish(color=color, width=3, lineJoin=1, closePath=True, oc=oc)
            shape.commit()
            if truth(record.get("ShowTimestamp"), False):
                text = record.get("TimeStampStr", "")
                if text:
                    center = fitz.Point((min(p.x for p in mapped) + max(p.x for p in mapped)) / 2,
                                        (min(p.y for p in mapped) + max(p.y for p in mapped)) / 2)
                    draw_text(page, center, fitz.Point(1, 0), text, 12,
                              fitz.Font("helv"), (0, 0, 0), derot, oc)
                    # Protect timestamp extent too (it can exceed a tiny cloud).
                    tw = fitz.get_text_length(text, fontsize=12)
                    content_points.extend([(center + fitz.Point(-tw / 2, -9)) * derot,
                                           (center + fitz.Point(tw / 2, 9)) * derot])
        else:
            a, b = mapped
            length = abs(b - a)
            direction = (b - a) / length
            normal = fitz.Point(-direction.y, direction.x)
            label = record.get("PrimaryLabel", "")
            color_key = "StampColor" if kind == "RubberStamp" else "TabColor"
            color = color_parser(record.get(color_key) or ("32768" if kind == "RubberStamp" else "65535"))
            text_color = color_parser(record.get("LabelColor") or "0")
            font, fallback = label_font("stencil" if kind == "RubberStamp" and truth(record.get("StenciledFont"), False)
                                        else "stamp" if kind == "RubberStamp" else "tab")
            if fallback:
                warnings.append(f"{name}: native font unavailable; using Helvetica Bold.")
            lines = label.replace("[[NL]]", "\n").splitlines() or [""]
            if kind == "RubberStamp":
                width_at_one = max([font.text_length(line, fontsize=1) for line in lines] + [1])
                size = length * .90 / width_at_one
                height = size * (1.25 + (len(lines) - 1) * 1.15)
                center = (a + b) / 2
                vertices = [a - normal * (height / 2), b - normal * (height / 2),
                            b + normal * (height / 2), a + normal * (height / 2)]
                polygon(vertices, color if truth(record.get("Bordered")) else None,
                        color, .30 * alpha_scale,
                        3 if truth(record.get("ThickerBorder"), False) else 1.5)
                if truth(record.get("Autosize"), False):
                    warnings.append(f"{name}: stamp Autosize is approximated from its two control points.")
            else:
                # Native first point is the tip, second point is the tail.
                tip, tail = a, b
                direction = (tip - tail) / length
                normal = fitz.Point(-direction.y, direction.x)
                height = length / 9
                neck = tip - direction * (height * .85)
                vertices = [tail - normal * (height / 2), neck - normal * (height / 2),
                            tip, neck + normal * (height / 2), tail + normal * (height / 2)]
                polygon(vertices, (0, 0, 0), color, .5 * alpha_scale, 2.4)
                fold_color = tuple((.5 * channel + .5) * .5 for channel in color)
                polygon([neck - normal * (height / 2), tip, neck + normal * (height / 2)],
                        (0, 0, 0), fold_color, 1, .75)
                center = (tail + neck) / 2 + direction * (height * .85 * .25)
                width_at_one = max([font.text_length(line, fontsize=1) for line in lines] + [1])
                size = min(height * .65 / len(lines), length * .78 / width_at_one)
                if not truth(record.get("Autosize")):
                    warnings.append(f"{name}: fixed-size sticky tab is approximated from its two control points.")
            draw_text(page, center, direction, label, size, font, text_color, derot, oc)
            if truth(record.get("ShowTimestamp"), False) and record.get("TimeStampStr"):
                # Keep the timestamp separate and searchable; native placement
                # for this option was not present in the supplied print.
                stamp_text = record["TimeStampStr"]
                ts = min(12, height * .2)
                ts_center = center + normal * (height / 2 + ts)
                draw_text(page, ts_center, direction, stamp_text, ts,
                          fitz.Font("helv"), text_color, derot, oc)
                tw = fitz.get_text_length(stamp_text, fontsize=ts)
                for dx in (-tw / 2, tw / 2):
                    for dy in (-ts, ts):
                        content_points.append((ts_center + direction * dx + normal * dy) * derot)
                warnings.append(f"{name}: timestamp placement is approximate.")
        if content_points:
            xs, ys = [p.x for p in content_points], [p.y for p in content_points]
            protected.append(fitz.Rect(min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2))
    return protected, warnings
