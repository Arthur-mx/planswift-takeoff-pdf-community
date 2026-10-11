# Adding screenshots

This folder contains privacy-edited interface images. Project text and drawing content are covered, and minor visual details may differ from the application. The main README uses the synthetic demo for its legend showcase. Use short, descriptive filenames such as `legend-defaults.png` so the links remain easy to maintain.

## Upload through GitHub

1. Open [this screenshot folder on GitHub](https://github.com/Arthur-mx/planswift-takeoff-pdf-community/tree/main/docs/screenshots).
2. Choose **Add file → Upload files**. Drag your PNG or JPG into the upload area.
3. Add a short commit message, such as “Add legend defaults screenshot,” and commit the upload.
4. Open the [main README](https://github.com/Arthur-mx/planswift-takeoff-pdf-community/blob/main/README.md) and click the pencil to edit it.
5. Add an image using the example below, replacing the filename and description. Use **Preview** to check it, then commit the change.

```markdown
![Visual legend defaults editor](docs/screenshots/legend-defaults.png)
```

To let readers click a smaller displayed image and open the original, use:

```markdown
[![Visual legend defaults editor](docs/screenshots/legend-defaults.png)](docs/screenshots/legend-defaults.png)
```

These paths are relative to the **root README.md**. If you rename or remove an image, update every README link that uses it. Keep filenames lowercase and use hyphens instead of spaces.

## Pick a useful screenshot

- Show one feature clearly, with enough context to understand it.
- Prefer a synthetic sample project when making new examples. Share real project drawings only when you have permission; check for client information, file paths, and title-block details before publishing.
- Redact before uploading. Cover confidential content completely rather than using a light blur. Check thumbnails, room geometry, quantities, captions, and screen edges as well as names and addresses. Replacing a published image does not erase earlier copies in Git history or other people's downloads.
- Use actual application output. A screenshot of a PDF shows the controls, but readers need the downloadable PDF to try them.
- Write alternative text that explains what the image demonstrates.
- For a replacement screenshot, keep the same filename to preserve the existing links.

## Current gallery

| File | Feature |
| --- | --- |
| `export-window.png` | Page tree, export options, and selected page order |
| `interactive-legend.png` | Searchable legend and PDF layer controls |
| `preview-thumbnails.png` | Preview with page thumbnails |
| `preview-list.png` | Preview with a compact sheet list |

GitHub's own instructions: [uploading files](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository) and [README images and relative links](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes).
