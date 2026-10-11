TAKEOFF PDF COMMUNITY - 1.0.1
An independent, unofficial PlanSwift PDF export plugin.

START HERE
1. Read INSTALL.txt and run Install.cmd from the extracted ZIP.
2. Import the separately distributed SwiftPluginPackage into PlanSwift.
   Maintainers building that package use the separate full PlanSwift Script TXT.
3. Open a job, choose pages, and use Preview PDF before saving.

WHAT IT DOES
- Searchable, selectable legend text with page quantities and units.
- One PDF layer per takeoff item, with clickable eye controls in the legend.
- Click a legend description to highlight that item's tracing with point markers.
- Hide all / Show all takeoff layers, plus Clear highlight.
- Automatic legend placement or placement from the page's PlanSwift Legend box.
- Takeoff outlines, fills, hatch patterns, count symbols, dimensions, notes and
  highlighters; a page-multiplier badge that avoids traced geometry and legends.
- Page tree with search, persistent Print flags, custom export order and defaults.
- Raster drawing backgrounds, or original source PDFs with sheet-name/page-label
  matching and source checks. Export location can differ from the source folder.
- Finished-export preview with remembered Thumbnails/List view, multi-selection,
  drag reordering, removal, Undo/Redo, and return to the print window.
- Drawing only, visible items or all items; optional legend and drawing greyscale.

COMMUNITY EDITION
No custom linked-takeoff property is required. Only items owning sections are
exported. There is no custom building-status exclusion and no automatic linked-
item recoloring. Existing PlanSwift colors are retained. It does not use personal
workstation paths, client examples or another installed plugin's icon assets.
This edition has separate installed files and preferences from the original
private exporter.

PDF READER SUPPORT
The existing legend interactions have been user-tested in Bluebeam Revu and
PDF-XChange. This community packaging still needs its first end-to-end test in
PlanSwift. Other viewers may ignore optional-content actions; browser previews
are not the compatibility benchmark. Open examples/Interactive Demo.pdf in a
layer-capable desktop reader. The built-in exporter preview displays the finished
appearance; interact with legend links in the saved PDF reader.

OPEN SOURCE
This project's supplied code and original community artwork are licensed under
GNU AGPL version 3 only. See LICENSE.txt and THIRD_PARTY_NOTICES.txt. No personal
author name is published. PlanSwift itself is proprietary and is not included.
The separate PlanSwift Script TXT is part of the corresponding source: distribute
it alongside the ZIP and any SwiftPluginPackage, without encrypting the script.
See SOURCE_REVIEW.txt for the inherited-code replacement review, and
PUBLISHING.txt for host-testing and release checks.

MORE
INSTALL.txt - installation and common problems
USER_GUIDE.txt - source setup, legend controls and preview workflow
DEVELOPER_STARTER_PROMPT.txt - context to continue development
CONTRIBUTING.txt - test and contribution workflow
REDDIT_POST.txt - editable announcement draft
PLUGIN_EXPORT_FIELDS.txt - suggested plugin export metadata
RELEASE_NOTES.txt - edition differences and validation limits

REPOSITORY SOURCE
The full PlanSwift bridge is included here as:
  Takeoff PDF Community - PlanSwift Script.txt
Create/import the PlanSwift plugin separately as described in INSTALL.txt.
This repository contains community 1.0.1, not the newer private-only features.
