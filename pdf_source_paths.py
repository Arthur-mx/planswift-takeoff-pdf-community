# SPDX-License-Identifier: AGPL-3.0-only
"""Source matching and validation shared by the export window and renderer."""
import os
import re
import fitz

from pdf_source_labels import source_label_index, match_source_label, label_key


def clean_sheet_id(page_name):
    """Page name -> sheet ID. Strips a leading duplicate marker '*' (a '*'
    prefix flags a duplicate page and is NOT part of the sheet number), then
    takes the text before the first '|' and trims.
      '*A-105 | 8th' -> 'A-105'      'A-102.00 | 3/16\" = 1' 0\"' -> 'A-102.00'
    """
    s = (page_name or "").strip().lstrip("*").strip()
    return s.split("|", 1)[0].strip()


def resolve_source_pdf(folder, sheet_id):
    """Match sheet ID to a PDF stem in folder. Exact stem (case-insensitive),
    then normalized (spaces stripped), then unique delimited prefix; else None.
    A-1 must never match A-10. Ambiguous matches are never chosen arbitrarily.
    """
    if not folder or not os.path.isdir(folder):
        return None, "folder missing: %r" % folder
    if not sheet_id.strip():
        return None, "empty sheet ID"
    pdfs = [f for f in os.listdir(folder) if f.lower().endswith(".pdf")
            and os.path.isfile(os.path.join(folder, f))]
    stems = {os.path.splitext(f)[0]: f for f in pdfs}

    norm = re.sub(r"\s+", "", sheet_id).casefold()
    for match in (
            lambda s: s.casefold() == sheet_id.casefold(),
            lambda s: re.sub(r"\s+", "", s).casefold() == norm,
            lambda s: s.casefold().startswith(sheet_id.casefold())
                      and len(s) > len(sheet_id)
                      and s[len(sheet_id)] in " -_(["):
        hits = [f for stem, f in stems.items() if match(stem)]
        if len(hits) == 1:
            return os.path.join(folder, hits[0]), None
        if len(hits) > 1:
            return None, "ambiguous PDF match: %s" % ", ".join(sorted(hits))
    return None, "no PDF stem matches %r" % sheet_id


def resolve_page_folder(project_root, takeoff_path):
    """Map a page's PlanSwift TakeoffPath to its on-disk split folder.

    TakeoffPath is a PlanSwift-tree path to the page's parent document, e.g.
    '..\\Pages\\Plans & Docs\\05-31-26\\Blueprints'. On disk the same tree is
    mirrored under the project root, and every imported document has a
    same-named subfolder holding its per-sheet split PDFs. So: take everything
    after the '\\Pages\\' segment and join it onto the project root.
    Returns (folder_path, err).
    """
    if not project_root:
        return None, "no project root provided"
    tp = (takeoff_path or "").strip()
    if not tp:
        return None, "page has no TakeoffPath"
    norm = tp.replace("/", "\\")                    # manifest is Windows-authored
    marker = "\\Pages\\"
    i = norm.lower().find(marker.lower())
    if i != -1:
        rel = norm[i + len(marker):]
    else:                                           # defensive fallback
        rel = norm
        while rel.startswith("..\\"):
            rel = rel[3:]
    rel = rel.strip("\\")
    if not rel:
        return None, "TakeoffPath gave an empty relative path: %r" % tp
    if any(part in (".", "..") or ":" in part for part in rel.split("\\")):
        return None, "TakeoffPath is not a relative project folder: %r" % tp
    folder = os.path.join(project_root, *rel.split("\\"))
    return folder, None


def validate_source_page(source, source_page=None, expected_label=None, page_data=None):
    if not source.is_pdf:
        raise ValueError("source file is not a PDF")
    if source.needs_pass:
        raise ValueError("source PDF is password protected")
    if source_page is None:
        if len(source) != 1:
            raise ValueError("source PDF has %d pages; expected one PDF per sheet. "
                             "For a labeled drawing set, enable page-label matching." % len(source))
        source_page = 0
    else:
        if not source.get_page_labels() or not expected_label:
            raise ValueError("drawing-set matching requires PDF page labels")
        if source_page < 0 or source_page >= len(source):
            raise ValueError("matched source page no longer exists; run the export again")
        if label_key(source[source_page].get_label()) != label_key(expected_label):
            raise ValueError("source page label changed; run the export again")
    sheet = source[source_page]
    rect = sheet.rect
    if rect.is_empty or rect.is_infinite:
        raise ValueError("source PDF has invalid page dimensions")
    if page_data is not None:
        pw = float(page_data.get("PageWidthPx", 0) or 0)
        ph = float(page_data.get("PageHeightPx", 0) or 0)
        if pw <= 0 or ph <= 0:
            raise ValueError("TIFF dimensions unavailable for aligning source PDF takeoffs")
        ratio = (rect.width / rect.height) / (pw / ph)
        if abs(ratio - 1) > 0.01:
            raise ValueError("source PDF aspect/orientation differs from PlanSwift TIFF "
                             "(PDF %.2f x %.2f pt; TIFF %.0f x %.0f px)"
                             % (rect.width, rect.height, pw, ph))
    return sheet, rect, source_page


class SourceResolver:
    def __init__(self, root, labels=False):
        self.root, self.labels = root, labels
        self.label_indexes = {}

    def match(self, takeoff_path, sheet_id):
        try:
            folder, error = resolve_page_folder(self.root, takeoff_path)
            if error:
                return None, error
            if self.labels:
                if folder not in self.label_indexes:
                    self.label_indexes[folder] = source_label_index(folder)
                index, error = self.label_indexes[folder]
                return (None, error) if error else match_source_label(index, sheet_id)
            path, error = resolve_source_pdf(folder, sheet_id)
            return ((path, None, None), None) if path else (None, error)
        except (OSError, ValueError) as exc:
            return None, str(exc)

    @staticmethod
    def check(reference, page_data=None):
        with fitz.open(reference[0]) as source:
            sheet, _, _ = validate_source_page(source, reference[1], reference[2], page_data)
            # Read page resources/content as well as the file header.
            sheet.get_displaylist()


def check_source_pages(root, pages, labels=False, progress=None):
    """Check the selected sheets only. Return successful matches and all issues."""
    if not root or not os.path.isabs(root) or not os.path.isdir(root):
        return [], [("Source folder", "Choose an accessible project folder containing Plans & Docs.")]
    resolver = SourceResolver(root, labels)
    matches, issues = [], []
    for index, page in enumerate(pages):
        name = page.get("PageName", "?")
        if progress:
            progress(index + 1, len(pages), name)
        reference, error = resolver.match(page.get("TakeoffPath", ""), clean_sheet_id(name))
        if reference:
            try:
                resolver.check(reference)
            except Exception as exc:
                error = str(exc)
        if error:
            issues.append((name, error))
        else:
            matches.append((name, reference))
    return matches, issues

