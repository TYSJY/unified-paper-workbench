# Drawing Step 04: Revise One Figure from the Previous Figure Package

## Input contract

- Upload the `figure_package.zip` returned by Drawing Step 03 and state which figure to revise and what must change.
- This ZIP is the previous step's single output and is the primary input for this step.
- If the interface cannot read ZIP files, upload only `figure_revision_handoff.md`, the target figure, and the target figure's raw PNG or data/script files extracted from that ZIP.
- Do not require the original paper, Step 01 board, Step 02 workflow, directory or naming metadata.
- If the target figure or requested change is ambiguous, ask only for the target and change.
- Do not claim to access the user's local filesystem.

## Source of truth

Use, in order:

1. `figure_revision_handoff.md`;
2. target entry in `submission_manifest.json`;
3. target raw PNG or normalized data and plotting script;
4. `paper_with_figures.tex` caption, claim and terminology;
5. the operator's revision request.

Preserve the established visual thesis, claims, data and caption intent unless the request explicitly changes them.

## Classify the target

### Conceptual schematic

- Use ChatGPT Image Generation to edit the uploaded raw PNG or regenerate from the recorded full prompt.
- The raw PNG is required for a pixel-level edit. If it is missing, either request it or obtain explicit permission for full regeneration.
- Keep labels short.
- Generate the complete visible figure; do not add semantic labels, arrows, boxes or titles with code afterward.
- Visually inspect all text, arrows and structure.
- Preserve the revised raw PNG and PDF wrapper.

### Quantitative data figure

- Use the normalized CSV and plotting script from the package.
- If they are missing, extract exact values only from authoritative tables or `CONFIRMED_DATA` included in the package.
- Never digitize uncertain values from the old plot or guess missing points.
- Use Data Analysis / Python, PGFPlots or the recorded reproducible renderer.
- Preserve values unless the user's request supplies corrected authoritative data.
- Return the revised script, data, PNG and PDF.

### Hybrid figure

- Revise conceptual panels with ChatGPT Image Generation.
- Revise quantitative panels with their data workflow.
- Keep panels separate and reassemble reproducibly.
- Never let image generation redraw numeric panels.

### Table

- Revise the LaTeX table in `paper_with_figures.tex`; do not convert it to an image.

## Mandatory re-upload checkpoint

If a newly generated conceptual PNG is not visible to the packaging workspace:

1. pause;
2. ask the user to download and re-upload it under the exact target filename;
3. do not claim the revised package or PDF is complete before re-upload.

## Review criteria

Before revising, assess:

1. clear visual thesis;
2. publication-quality rather than slide-like appearance;
3. excessive box-and-arrow structure;
4. panel clarity;
5. text density;
6. label readability;
7. traceability of all numbers;
8. two-column readability;
9. caption/figure agreement;
10. consistency with the recorded visual style.

## Output contract

Preferred output: one flat ZIP named `figure_revision_package.zip` containing only actual revised or required files:

- revised raw PNG when conceptual;
- revised final PNG and PDF;
- revised normalized data and plotting script when quantitative;
- updated `paper_with_figures.tex` if filename, caption, label or layout changed;
- updated `figure_revision_handoff.md`;
- updated `submission_manifest.json`;
- a compiled PDF only if a real compiler was run successfully.

If ZIP creation is unavailable, provide the actual files individually. Text may fall back to complete code blocks. Never represent a missing image or PDF with prose.

At the end, report:

- target figure;
- exact requested change;
- renderer actually used;
- source data or source prompt;
- files actually generated;
- visual/data validation performed;
- whether re-upload was required;
- any unresolved text, data, conversion or compilation limitation.

Do not revise any other figure unless required to keep a shared legend, caption or hybrid layout consistent.
