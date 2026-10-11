# SPDX-License-Identifier: AGPL-3.0-only
"""Convert section records from the community bridge into renderer item groups.

The host emits one item record per tracing. This module owns grouping and the
Qty x sum(Takeoff %) calculation; the legacy Excel export algorithm is not used.
"""
from decimal import Decimal, InvalidOperation


def _number(value, field):
    # Pascal FloatToStr can use a comma decimal separator. It does not group digits.
    text = str(value).strip().replace(',', '.')
    try:
        result = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f'Invalid {field} in section export: {value!r}') from error
    if not result.is_finite():
        raise ValueError(f'Non-finite {field} in section export.')
    return result


def normalize_section_records(job):
    """Retain first-seen item order, collect its sections, and allocate page Qty.

    Version-1 grouped manifests remain unchanged for existing tools and fixtures.
    Private section-record fields are removed after conversion so a second pass
    is harmless. Unknown layouts fail explicitly instead of silently losing data.
    """
    layout = job.get('ItemLayout', '')
    if layout in ('', 'GroupedItemsV1'):
        return job
    if layout != 'SectionRecordsV1':
        raise ValueError(f'Unsupported manifest item layout: {layout}')
    pages = []
    for page in job.get('pages', []):
        groups = {}
        for record in page.get('items', []):
            identity = record.get('ItemGUID', '').strip().strip('{}').upper()
            if not identity:
                raise ValueError('A section record is missing its parent ItemGUID.')
            parent_qty = _number(record.get('ParentQty', ''), 'parent Qty')
            share = _number(record.get('SectionShare', ''), 'Takeoff %')
            if identity not in groups:
                item = {key: value for key, value in record.items()
                        if key not in ('ParentQty', 'SectionShare', 'sections')}
                item['sections'] = []
                groups[identity] = [item, parent_qty, Decimal(0)]
            item, original_qty, fraction = groups[identity]
            if original_qty != parent_qty or any(item.get(key, '') != record.get(key, '')
                                                  for key in ('Units', 'ItemType', 'Color')):
                raise ValueError('Takeoff item changed during export; run again: ' + identity)
            item['sections'].extend(record.get('sections', []))
            groups[identity][2] = fraction + share
        items = []
        for item, parent_qty, fraction in groups.values():
            total = parent_qty * fraction
            if total == 0:
                continue
            item['PageTotal'] = format(total, 'f')
            item['PageTotalBasis'] = 'Qty'
            items.append(item)
        pages.append(dict(page, items=items))
    return dict(job, pages=pages, ItemLayout='GroupedItemsV1')
