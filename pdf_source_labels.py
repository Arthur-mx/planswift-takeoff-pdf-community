# SPDX-License-Identifier: AGPL-3.0-only
"""Strict PDF page-label matching shared by the picker and renderer."""
from pathlib import Path
import unicodedata

import fitz


def label_key(value):
    # Keep punctuation and internal spaces significant: A-1 is not A-10.
    return unicodedata.normalize("NFC", value.strip()).casefold()


def read_page_labels(path):
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("PDF is password protected; use an unlocked source copy.")
        if not doc.is_pdf or not doc.get_page_labels():
            raise ValueError("PDF has no page labels. Set each page label to its sheet ID in your PDF editor.")
        labels = [page.get_label() for page in doc]
        blank = [str(i + 1) for i, label in enumerate(labels) if not label.strip()]
        if blank:
            raise ValueError("Missing page labels on PDF pages: " + ", ".join(blank))
        return labels


def source_label_index(folder):
    """Index only the matching document location, never other revision folders.

    Accept PDFs inside the mirrored folder and an exact same-named PDF beside
    it (the natural on-disk location of an unsplit imported drawing set).
    Unreadable/unlabeled candidates prevent matching rather than hiding a
    possible conflicting source. Duplicate labels remain visible as conflicts.
    """
    folder = Path(folder)
    candidates = []
    if folder.is_dir():
        candidates.extend(p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() == ".pdf")
    if folder.parent.is_dir():
        expected = (folder.name + ".pdf").casefold()
        candidates.extend(p for p in folder.parent.iterdir() if p.is_file() and p.name.casefold() == expected)
    if not candidates:
        return {}, "no drawing-set PDFs found in %s or beside it as %s.pdf" % (folder, folder.name)
    index = {}
    problems = []
    seen_paths = set()
    for path in sorted(candidates):
        identity = str(path.resolve()).casefold()
        if identity in seen_paths:
            continue
        seen_paths.add(identity)
        try:
            labels = read_page_labels(path)
        except Exception as exc:
            problems.append("%s: %s" % (path.name, exc))
            continue
        for page_number, label in enumerate(labels):
            index.setdefault(label_key(label), []).append((str(path), page_number, label))
    if problems:
        return {}, "; ".join(problems)
    return index, None


def match_source_label(index, sheet_id):
    key = label_key(sheet_id)
    if not key:
        return None, "empty PlanSwift sheet ID"
    matches = index.get(key, [])
    if not matches:
        return None, "no page label matches sheet ID %r" % sheet_id
    if len(matches) > 1:
        details = ["%s (page %d, label %s)" % (Path(path).name, page + 1, label)
                   for path, page, label in matches]
        return None, "duplicate page label for %r: %s" % (sheet_id, "; ".join(details))
    return matches[0], None
